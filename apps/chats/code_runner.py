import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)


def _truncate(data: bytes, limit: int) -> str:
    if data is None:
        return ''
    truncated = data[:limit]
    text = truncated.decode('utf-8', errors='replace')
    if len(data) > limit:
        text += '\n...[output truncated]'
    return text


def _minimal_env():
    """Env без секретов Django/БД/MinIO — только то, что нужно Python на ОС."""
    env = {}
    for key in ('PATH', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL'):
        val = os.environ.get(key)
        if val:
            env[key] = val
    # На Windows Python часто нужен COMSPEC / PATHEXT
    for key in ('COMSPEC', 'PATHEXT', 'USERPROFILE'):
        val = os.environ.get(key)
        if val:
            env[key] = val
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


def _linux_preexec(memory_bytes: int):
    def _limit():
        try:
            import resource
            soft = memory_bytes
            hard = memory_bytes
            resource.setrlimit(resource.RLIMIT_AS, (soft, hard))
            resource.setrlimit(resource.RLIMIT_DATA, (soft, hard))
            # CPU soft-ish: secondary to wall-clock timeout
            resource.setrlimit(resource.RLIMIT_CPU, (settings.CODE_RUN_TIMEOUT_SEC + 1, settings.CODE_RUN_TIMEOUT_SEC + 2))
        except Exception as exc:  # noqa: BLE001
            logger.warning('Failed to set resource limits: %s', exc)

    return _limit


def _windows_job_memory_limit(proc, memory_bytes: int) -> bool:
    """Assign process to a Job Object with memory limit. Returns False if unavailable."""
    if os.name != 'nt':
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ('ReadOperationCount', ctypes.c_uint64),
                ('WriteOperationCount', ctypes.c_uint64),
                ('OtherOperationCount', ctypes.c_uint64),
                ('ReadTransferCount', ctypes.c_uint64),
                ('WriteTransferCount', ctypes.c_uint64),
                ('OtherTransferCount', ctypes.c_uint64),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ('PerProcessUserTimeLimit', ctypes.c_int64),
                ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', wintypes.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t),
                ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t),
            ]

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return False

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        info.ProcessMemoryLimit = memory_bytes
        info.JobMemoryLimit = memory_bytes

        ok = kernel32.SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(handle)
            return False

        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        process_handle = kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE,
            False,
            proc.pid,
        )
        if not process_handle:
            kernel32.CloseHandle(handle)
            return False

        assigned = kernel32.AssignProcessToJobObject(handle, process_handle)
        kernel32.CloseHandle(process_handle)
        # Keep job handle open until process exits so KILL_ON_JOB_CLOSE works;
        # store on process object for GC after communicate.
        proc._monica_job_handle = handle  # noqa: SLF001
        return bool(assigned)
    except Exception as exc:  # noqa: BLE001
        logger.warning('Windows Job Object memory limit failed: %s', exc)
        return False


def run_python_source(source_bytes: bytes, filename: str = 'script.py') -> dict:
    """
    Запуск пользовательского Python в изолированном subprocess.
    Возвращает stdout/stderr/exit_code/timed_out/memory_exceeded.
    """
    timeout = settings.CODE_RUN_TIMEOUT_SEC
    memory_bytes = settings.CODE_RUN_MEMORY_MB * 1024 * 1024
    max_out = settings.CODE_RUN_MAX_OUTPUT_BYTES
    max_src = settings.CODE_RUN_MAX_SOURCE_BYTES

    if not source_bytes:
        raise ValueError('Пустой файл')
    if len(source_bytes) > max_src:
        raise ValueError(f'Исходник больше {max_src} байт')

    safe_name = Path(filename or 'script.py').name
    if not safe_name.lower().endswith('.py'):
        safe_name = 'script.py'

    tmp_dir = tempfile.mkdtemp(prefix='monica_code_')
    script_path = Path(tmp_dir) / safe_name
    timed_out = False
    memory_exceeded = False
    exit_code = -1
    stdout = b''
    stderr = b''

    try:
        script_path.write_bytes(source_bytes)

        preexec = _linux_preexec(memory_bytes) if os.name != 'nt' else None
        creationflags = 0
        if os.name == 'nt':
            # CREATE_NEW_PROCESS_GROUP — проще убивать по timeout
            creationflags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)

        proc = subprocess.Popen(
            [sys.executable, '-I', str(script_path)],
            cwd=tmp_dir,
            env=_minimal_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=preexec,
            creationflags=creationflags,
        )

        if os.name == 'nt':
            if not _windows_job_memory_limit(proc, memory_bytes):
                logger.warning('RAM limit via Job Object unavailable; relying on timeout')

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            stdout, stderr = proc.communicate()
            exit_code = -1
        finally:
            job = getattr(proc, '_monica_job_handle', None)
            if job:
                try:
                    import ctypes
                    ctypes.WinDLL('kernel32').CloseHandle(job)
                except Exception:  # noqa: BLE001
                    pass

        # Windows / Linux: типичные коды при OOM
        if exit_code in (137, 139) or (os.name == 'nt' and exit_code in (0xC0000017, -1073741801)):
            memory_exceeded = True

        return {
            'stdout': _truncate(stdout, max_out),
            'stderr': _truncate(stderr, max_out),
            'exit_code': exit_code,
            'timed_out': timed_out,
            'memory_exceeded': memory_exceeded,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_javascript_source(source_bytes: bytes, filename: str = 'script.js') -> dict:
    """
    Запуск пользовательского JavaScript через Node.js в изолированном subprocess.
    Те же лимиты, что и у Python.
    """
    timeout = settings.CODE_RUN_TIMEOUT_SEC
    memory_bytes = settings.CODE_RUN_MEMORY_MB * 1024 * 1024
    max_out = settings.CODE_RUN_MAX_OUTPUT_BYTES
    max_src = settings.CODE_RUN_MAX_SOURCE_BYTES

    if not source_bytes:
        raise ValueError('Пустой файл')
    if len(source_bytes) > max_src:
        raise ValueError(f'Исходник больше {max_src} байт')

    node_bin = shutil.which('node')
    if not node_bin:
        raise ValueError('Node.js не установлен на сервере')

    safe_name = Path(filename or 'script.js').name
    if not safe_name.lower().endswith('.js'):
        safe_name = 'script.js'

    tmp_dir = tempfile.mkdtemp(prefix='monica_js_')
    script_path = Path(tmp_dir) / safe_name
    timed_out = False
    memory_exceeded = False
    exit_code = -1
    stdout = b''
    stderr = b''

    try:
        script_path.write_bytes(source_bytes)

        preexec = _linux_preexec(memory_bytes) if os.name != 'nt' else None
        creationflags = 0
        if os.name == 'nt':
            creationflags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)

        proc = subprocess.Popen(
            [node_bin, str(script_path)],
            cwd=tmp_dir,
            env=_minimal_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=preexec,
            creationflags=creationflags,
        )

        if os.name == 'nt':
            if not _windows_job_memory_limit(proc, memory_bytes):
                logger.warning('RAM limit via Job Object unavailable; relying on timeout')

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            stdout, stderr = proc.communicate()
            exit_code = -1
        finally:
            job = getattr(proc, '_monica_job_handle', None)
            if job:
                try:
                    import ctypes
                    ctypes.WinDLL('kernel32').CloseHandle(job)
                except Exception:  # noqa: BLE001
                    pass

        if exit_code in (137, 139) or (os.name == 'nt' and exit_code in (0xC0000017, -1073741801)):
            memory_exceeded = True

        return {
            'stdout': _truncate(stdout, max_out),
            'stderr': _truncate(stderr, max_out),
            'exit_code': exit_code,
            'timed_out': timed_out,
            'memory_exceeded': memory_exceeded,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
