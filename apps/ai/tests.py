from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.ai.client import (
    cosine_similarity,
    estimate_messages_tokens,
    fit_messages_to_token_budget,
    should_continue_final_message,
)
from apps.ai.models import UserStyleProfile
from apps.ai.services import (
    append_style_sample,
    build_completion_messages,
    build_forced_continuation,
    day_transcript_to_messages,
    infer_length_target,
    parse_reply_intent,
    recent_focus_turns,
    sanitize_suggestion,
    select_style_samples,
    strip_draft_prefix,
)


class StyleServicesTests(TestCase):
    def test_forced_continuation_adds_virtual_comma_for_complete_short_draft(self):
        messages = [
            {'role': 'system', 'content': 'continue'},
            {'role': 'assistant', 'content': 'ну как'},
        ]
        forced, prefix = build_forced_continuation(messages, 'ну как')
        self.assertEqual(forced[-1]['content'], 'ну как,')
        self.assertEqual(prefix, ', ')
        self.assertEqual(messages[-1]['content'], 'ну как')

    def test_continue_final_message_only_for_non_empty_assistant_prefill(self):
        self.assertTrue(should_continue_final_message([
            {'role': 'user', 'content': 'context'},
            {'role': 'assistant', 'content': 'current draft'},
        ]))
        self.assertFalse(should_continue_final_message([
            {'role': 'assistant', 'content': ''},
        ]))
        self.assertFalse(should_continue_final_message([
            {'role': 'user', 'content': 'current draft'},
        ]))

    def test_prompt_budget_trims_earliest_context_and_keeps_current_draft(self):
        current_draft = 'полный текущий черновик пользователя'
        messages = [
            {'role': 'system', 'content': 'continue safely'},
            {'role': 'user', 'content': 'СТАРАЯ ИСТОРИЯ ' * 500},
            {'role': 'assistant', 'content': current_draft},
        ]
        fitted = fit_messages_to_token_budget(
            messages,
            completion_tokens=100,
            context_window_tokens=500,
            reserve_tokens=50,
        )
        self.assertLessEqual(estimate_messages_tokens(fitted), 350)
        self.assertTrue(fitted[1]['content'].startswith('[earlier context trimmed]'))
        self.assertEqual(fitted[-1]['content'], current_draft)

    def test_strip_draft_prefix(self):
        self.assertEqual(strip_draft_prefix('hello world', 'hello '), 'world')
        self.assertEqual(strip_draft_prefix('world', 'hello '), 'world')

    def test_sanitize_drops_reasoning_leaks(self):
        self.assertEqual(
            sanitize_suggestion('Хорошо, мне нужно продолжить черновик'),
            '',
        )
        self.assertEqual(sanitize_suggestion(', уже надоело'), ', уже надоело')

    def test_infer_length_and_day_context_in_prompt(self):
        self.assertEqual(infer_length_target('ок давай'), 'short')
        day = (
            'Собеседник: привет, как дела?\n'
            'Я: норм\n'
            'Собеседник: можешь завтра в 5?\n'
        )
        self.assertEqual(infer_length_target('давай', day), 'short')
        draft = 'давай тогда в пять\nесли ок'
        msgs = build_completion_messages(
            draft,
            ['норм', 'ок'],
            {'tone': 'неформальный'},
            day_transcript=day,
            partner_notes='общаемся коротко',
            topic='встреча завтра',
            reply_goal='подтвердить время',
        )
        self.assertIn('Как я общаюсь с этим пользователем', msgs[1]['content'])
        self.assertIn('Смысл текущего ответа', msgs[1]['content'])
        self.assertIn('тема: встреча завтра', msgs[1]['content'])
        self.assertIn('цель_ответа: подтвердить время', msgs[1]['content'])
        self.assertIn('последние_реплики:', msgs[1]['content'])
        self.assertIn('можешь завтра в 5?', msgs[1]['content'])
        self.assertIn('length_target=', msgs[1]['content'])
        self.assertIn('цель_ответа', msgs[0]['content'])
        self.assertIn('последние 2–3 реплики', msgs[0]['content'])
        self.assertIn('активная тема', msgs[0]['content'])
        self.assertEqual(
            msgs[2:-2],
            [
                {'role': 'user', 'content': 'привет, как дела?'},
                {'role': 'assistant', 'content': 'норм'},
                {'role': 'user', 'content': 'можешь завтра в 5?'},
            ],
        )
        self.assertEqual(msgs[-2]['role'], 'user')
        self.assertIn('подтвердить время', msgs[-2]['content'])
        self.assertIn('можешь завтра в 5?', msgs[-2]['content'])
        self.assertEqual(msgs[-1]['role'], 'assistant')
        self.assertEqual(msgs[-1]['content'], draft)

    def test_build_completion_messages_topic_shift_and_related(self):
        draft = 'давай лучше про ужин'
        msgs = build_completion_messages(
            draft,
            [],
            {},
            day_transcript='Собеседник: что на ужин?\nЯ: не знаю',
            related_transcript='Собеседник: вчера тоже про еду говорили',
            topic='ужин',
            reply_goal='предложить пиццу',
            topic_shift=True,
        )
        self.assertIn('Тема недавно сменилась', msgs[0]['content'])
        self.assertIn('topic_shift: true', msgs[1]['content'])
        related_blocks = [m for m in msgs if 'Связанные реплики по теме' in m.get('content', '')]
        self.assertEqual(len(related_blocks), 1)
        self.assertIn('вчера тоже про еду', related_blocks[0]['content'])
        joined = '\n'.join(m.get('content', '') for m in msgs)
        self.assertNotIn('деплой', joined)
        self.assertIn('что на ужин?', joined)

    def test_recent_focus_turns_takes_last_n(self):
        day = (
            'Собеседник: один\n'
            'Я: два\n'
            'Собеседник: три\n'
            'Я: четыре\n'
            'Собеседник: пять\n'
        )
        self.assertEqual(
            recent_focus_turns(day, n=3),
            'Собеседник: три\nЯ: четыре\nСобеседник: пять',
        )

    def test_parse_reply_intent_valid_and_garbage(self):
        self.assertEqual(
            parse_reply_intent('{"topic":"деплой Моники","reply_goal":"объяснить что запушил"}'),
            {'topic': 'деплой Моники', 'reply_goal': 'объяснить что запушил', 'topic_shift': False},
        )
        self.assertEqual(
            parse_reply_intent(
                'Вот ответ:\n```json\n{"topic":"а","reply_goal":"б","topic_shift":true}\n```'
            ),
            {'topic': 'а', 'reply_goal': 'б', 'topic_shift': True},
        )
        # Prefill continuation suffix reconstructed into a full JSON object.
        self.assertEqual(
            parse_reply_intent(
                '{"topic":"дружеский разговор","reply_goal":"продолжить с юмором"}'
            ),
            {
                'topic': 'дружеский разговор',
                'reply_goal': 'продолжить с юмором',
                'topic_shift': False,
            },
        )
        self.assertEqual(
            parse_reply_intent(''),
            {'topic': '', 'reply_goal': '', 'topic_shift': False},
        )
        self.assertEqual(
            parse_reply_intent('not json at all'),
            {'topic': '', 'reply_goal': '', 'topic_shift': False},
        )
        self.assertEqual(
            parse_reply_intent('{bad'),
            {'topic': '', 'reply_goal': '', 'topic_shift': False},
        )

    def test_cosine_similarity_and_topic_boundary_helper(self):
        from apps.ai.embeddings import should_start_new_topic
        from types import SimpleNamespace

        self.assertGreater(cosine_similarity([1, 0], [1, 0]), 0.99)
        self.assertLess(cosine_similarity([1, 0], [0, 1]), 0.1)

        prev_msg = SimpleNamespace(created_at=None)
        cur_msg = SimpleNamespace(created_at=None)
        prev_emb = SimpleNamespace(embedding=[1.0, 0.0])
        self.assertFalse(
            should_start_new_topic(
                previous=prev_emb,
                previous_message=prev_msg,
                current_message=cur_msg,
                current_vector=[0.95, 0.05],
            )
        )
        self.assertTrue(
            should_start_new_topic(
                previous=prev_emb,
                previous_message=prev_msg,
                current_message=cur_msg,
                current_vector=[0.0, 1.0],
            )
        )

    def test_day_transcript_roles_and_consecutive_messages(self):
        transcript = (
            'Собеседник: один\n'
            'Собеседник: два\n'
            'Я: три\n'
            'Я: четыре\n'
        )
        self.assertEqual(
            day_transcript_to_messages(transcript),
            [
                {'role': 'user', 'content': 'один\nдва'},
                {'role': 'assistant', 'content': 'три\nчетыре'},
            ],
        )

    def test_append_and_select_samples(self):
        User = get_user_model()
        user = User.objects.create_user(
            email='ai@example.com',
            nickname='aiuser',
            password='testpass123',
            first_name='A',
            last_name='I',
        )
        append_style_sample(user, 'привет как дела')
        append_style_sample(user, 'норм всё ок бро')
        profile = UserStyleProfile.objects.get(user=user)
        self.assertEqual(len(profile.samples), 2)
        picked = select_style_samples(profile, 'привет')
        self.assertTrue(any('привет' in s for s in picked))


class CompleteApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            email='ai2@example.com',
            nickname='aiuser2',
            password='testpass123',
            first_name='A',
            last_name='I',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_style_toggle(self):
        response = self.client.get('/api/ai/style/')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['enabled'])
        response = self.client.patch('/api/ai/style/', {'enabled': False}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['enabled'])
