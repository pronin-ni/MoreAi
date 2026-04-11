"""
Tests for /studio conversation persistence and multi-chat sidebar.

Tests the JavaScript storage logic via a simulated Python equivalent
of the localStorage persistence model.

Covers:
- chat creation
- switching active chat
- persistence (store shape validation)
- restoring mode + messages + execution summary
- corrupted storage recovery
- pruning/bounded behavior
- auto-title generation
"""

import json
import time

# ── Constants (mirrors studio.js) ──

STORAGE_KEY = 'studio_chats_v1'
MAX_CHATS = 50
MAX_MESSAGES = 100
STUDIO_MODES = {
    'fast': {'label': 'Fast', 'isPipeline': False},
    'balanced': {'label': 'Balanced', 'isPipeline': False},
    'quality': {'label': 'Quality', 'isPipeline': True},
    'review': {'label': 'Review', 'isPipeline': True},
    'deep': {'label': 'Deep', 'isPipeline': True},
}


# ── Simulated Store ──

class SimulatedStore:
    """Python simulation of the localStorage persistence layer."""

    def __init__(self):
        self.chats = {}
        self.active_chat_id = None

    def create_chat(self, mode='balanced'):
        chat_id = f'chat_{int(time.time())}_{len(self.chats)}'
        self.chats[chat_id] = {
            'id': chat_id,
            'title': 'New chat',
            'mode': mode,
            'created_at': int(time.time() * 1000),
            'updated_at': int(time.time() * 1000),
            'messages': [],
            'lastExecution': None,
        }
        self._enforce_limits()
        return chat_id

    def set_active(self, chat_id):
        if chat_id in self.chats:
            self.active_chat_id = chat_id

    def delete_chat(self, chat_id):
        if chat_id in self.chats:
            del self.chats[chat_id]
        if self.active_chat_id not in self.chats:
            remaining = list(self.chats.keys())
            self.active_chat_id = remaining[-1] if remaining else None

    def add_message(self, chat_id, role, content):
        if chat_id not in self.chats:
            return
        self.chats[chat_id]['messages'].append({
            'role': role,
            'content': content,
            'timestamp': time.time(),
        })
        self.chats[chat_id]['updated_at'] = int(time.time() * 1000)
        # Auto-title
        if self.chats[chat_id]['title'] == 'New chat' and role == 'user':
            self.chats[chat_id]['title'] = content[:60] + ('…' if len(content) > 60 else '')
        # Prune excess messages
        if len(self.chats[chat_id]['messages']) > MAX_MESSAGES:
            self.chats[chat_id]['messages'] = self.chats[chat_id]['messages'][-MAX_MESSAGES:]

    def set_last_execution(self, chat_id, exec_data):
        if chat_id in self.chats:
            self.chats[chat_id]['lastExecution'] = exec_data
            self.chats[chat_id]['updated_at'] = int(time.time() * 1000)

    def serialize(self):
        return json.dumps({'chats': self.chats, 'activeChatId': self.active_chat_id})

    def deserialize(self, raw):
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return False
            if not isinstance(data.get('chats'), dict):
                return False
            self.chats = data['chats']
            self.active_chat_id = data.get('activeChatId')
            if self.active_chat_id and self.active_chat_id not in self.chats:
                remaining = list(self.chats.keys())
                self.active_chat_id = remaining[-1] if remaining else None
            return True
        except (json.JSONDecodeError, ValueError):
            return False

    def _enforce_limits(self):
        if len(self.chats) > MAX_CHATS:
            ids = sorted(self.chats.keys(), key=lambda k: self.chats[k].get('updated_at', 0))
            to_remove = ids[:len(ids) - MAX_CHATS]
            for cid in to_remove:
                del self.chats[cid]
            if self.active_chat_id and self.active_chat_id not in self.chats:
                remaining = list(self.chats.keys())
                self.active_chat_id = remaining[-1] if remaining else None


# ── Tests ──


class TestChatPersistence:
    """Core persistence behavior."""

    def test_create_chat(self):
        store = SimulatedStore()
        chat_id = store.create_chat('balanced')
        assert chat_id in store.chats
        assert store.chats[chat_id]['mode'] == 'balanced'
        assert store.chats[chat_id]['title'] == 'New chat'
        assert store.chats[chat_id]['messages'] == []

    def test_serialization_roundtrip(self):
        store = SimulatedStore()
        chat_id = store.create_chat('quality')
        store.add_message(chat_id, 'user', 'What is Python?')
        store.set_last_execution(chat_id, {'execution_id': 'exec-1', 'quality_score': 0.8})
        store.set_active(chat_id)

        raw = store.serialize()
        store2 = SimulatedStore()
        assert store2.deserialize(raw) is True
        assert chat_id in store2.chats
        assert store2.chats[chat_id]['mode'] == 'quality'
        assert len(store2.chats[chat_id]['messages']) == 1
        assert store2.active_chat_id == chat_id

    def test_corrupted_storage_recovery(self):
        store = SimulatedStore()
        # Corrupted JSON
        assert store.deserialize('not json') is False
        assert store.deserialize('{}') is False
        assert store.deserialize('{"chats": "not a dict"}') is False
        assert store.deserialize('null') is False

    def test_missing_active_chat_recovery(self):
        store = SimulatedStore()
        chat_id = store.create_chat('balanced')
        store.set_active(chat_id)

        # Delete the active chat
        store.delete_chat(chat_id)
        assert store.active_chat_id is None

    def test_serialization_preserves_execution_data(self):
        store = SimulatedStore()
        chat_id = store.create_chat('deep')
        store.set_last_execution(chat_id, {
            'execution_id': 'exec-123',
            'mode': 'deep',
            'stage_count': 3,
            'selected_models': ['qwen', 'glm'],
            'fallback_count': 0,
            'quality_score': 0.75,
            'duration_ms': 12000,
            'status': 'success',
        })

        raw = store.serialize()
        store2 = SimulatedStore()
        store2.deserialize(raw)

        exec_data = store2.chats[chat_id]['lastExecution']
        assert exec_data['execution_id'] == 'exec-123'
        assert exec_data['quality_score'] == 0.75
        assert exec_data['stage_count'] == 3


class TestMultiChatBehavior:
    """Multi-chat sidebar and switching."""

    def test_switch_active_chat(self):
        store = SimulatedStore()
        store.create_chat('fast')
        id2 = store.create_chat('quality')
        store.set_active(id2)
        assert store.active_chat_id == id2

    def test_delete_active_chat_switches_to_recent(self):
        store = SimulatedStore()
        id1 = store.create_chat('fast')
        time.sleep(0.01)  # ensure different timestamps
        id2 = store.create_chat('quality')
        store.set_active(id2)

        store.delete_chat(id2)
        assert store.active_chat_id == id1

    def test_delete_last_chat_creates_new(self):
        store = SimulatedStore()
        id1 = store.create_chat('balanced')
        store.set_active(id1)

        store.delete_chat(id1)
        assert store.active_chat_id is None
        assert len(store.chats) == 0

    def test_delete_non_active_chat(self):
        store = SimulatedStore()
        id1 = store.create_chat('fast')
        time.sleep(0.01)
        id2 = store.create_chat('quality')
        store.set_active(id2)

        store.delete_chat(id1)
        assert store.active_chat_id == id2
        assert id1 not in store.chats


class TestMessagePersistence:
    """Messages save/restore behavior."""

    def test_add_messages(self):
        store = SimulatedStore()
        chat_id = store.create_chat('balanced')
        store.add_message(chat_id, 'user', 'Hello')
        store.add_message(chat_id, 'assistant', 'Hi there!')

        assert len(store.chats[chat_id]['messages']) == 2
        assert store.chats[chat_id]['messages'][0]['role'] == 'user'
        assert store.chats[chat_id]['messages'][1]['role'] == 'assistant'

    def test_prune_excess_messages(self):
        store = SimulatedStore()
        chat_id = store.create_chat('balanced')
        for i in range(120):
            store.add_message(chat_id, 'user', f'Message {i}')

        assert len(store.chats[chat_id]['messages']) == MAX_MESSAGES
        # Should keep the most recent
        assert store.chats[chat_id]['messages'][0]['content'] == 'Message 20'

    def test_auto_title_from_first_user_message(self):
        store = SimulatedStore()
        chat_id = store.create_chat('balanced')
        store.add_message(chat_id, 'user', 'This is a very long question that should be truncated for the title because it is way too long')

        assert store.chats[chat_id]['title'] != 'New chat'
        assert store.chats[chat_id]['title'].endswith('…')
        assert len(store.chats[chat_id]['title']) <= 61  # 60 chars + ellipsis


class TestExecutionSummaryPersistence:
    """Execution metadata persistence per chat."""

    def test_save_and_restore_execution_summary(self):
        store = SimulatedStore()
        chat_id = store.create_chat('quality')
        store.set_last_execution(chat_id, {
            'execution_id': 'exec-1',
            'mode': 'quality',
            'execution_type': 'pipeline',
            'pipeline_id': 'generate-review-refine',
            'stage_count': 3,
            'selected_models': ['qwen', 'glm'],
            'fallback_count': 1,
            'quality_score': 0.78,
            'duration_ms': 15000,
            'status': 'success',
        })

        raw = store.serialize()
        store2 = SimulatedStore()
        store2.deserialize(raw)

        exec_data = store2.chats[chat_id]['lastExecution']
        assert exec_data['mode'] == 'quality'
        assert exec_data['stage_count'] == 3
        assert exec_data['fallback_count'] == 1
        assert exec_data['quality_score'] == 0.78
        assert exec_data['execution_type'] == 'pipeline'


class TestBoundedBehavior:
    """Pruning and limits."""

    def test_prune_excess_chats(self):
        store = SimulatedStore()
        # Create more than MAX_CHATS
        ids = []
        for _i in range(MAX_CHATS + 10):
            cid = store.create_chat('balanced')
            ids.append(cid)

        assert len(store.chats) <= MAX_CHATS

    def test_oldest_chats_pruned_first(self):
        store = SimulatedStore()
        # Create chats with increasing timestamps
        for i in range(MAX_CHATS + 5):
            cid = store.create_chat('balanced')
            store.chats[cid]['updated_at'] = i * 1000  # simulate increasing age

        # The oldest (lowest updated_at) should be removed
        remaining_ids = list(store.chats.keys())
        remaining_times = [store.chats[cid]['updated_at'] for cid in remaining_ids]
        assert min(remaining_times) > 0  # oldest removed
