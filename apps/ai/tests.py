from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.ai.client import estimate_messages_tokens, fit_messages_to_token_budget
from apps.ai.models import UserStyleProfile
from apps.ai.services import (
    append_style_sample,
    build_completion_messages,
    infer_length_target,
    sanitize_suggestion,
    select_style_samples,
    strip_draft_prefix,
)


class StyleServicesTests(TestCase):
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
        )
        user = msgs[1]['content']
        self.assertIn('Как я общаюсь с этим пользователем', user)
        self.assertIn('История общения за сегодня', user)
        self.assertIn('Моё текущее сообщение', user)
        self.assertIn('можешь завтра в 5?', user)
        self.assertIn('последнее_от_собеседника: можешь завтра в 5?', user)
        self.assertIn(draft, user)
        self.assertIn('length_target=', user)
        self.assertEqual(msgs[2]['role'], 'assistant')
        self.assertEqual(msgs[2]['content'], draft)

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
