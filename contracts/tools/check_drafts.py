"""Offline design checks. No service, SDK, database or hardware imports."""
import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parents[1] / 'drafts/operation-v0'


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('Non-finite JSON number')


def strict_numbers(value):
    if isinstance(value, float):
        raise ValueError('Floating-point values are outside this draft request format')
    if isinstance(value, dict):
        for child in value.values():
            strict_numbers(child)
    elif isinstance(value, list):
        for child in value:
            strict_numbers(child)


def load(text):
    value = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
    strict_numbers(value)
    return value


class OperationDraftChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load((ROOT / 'request.schema.json').read_text(encoding='utf-8'))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)
        cls.fixtures = {p.stem: load(p.read_text(encoding='utf-8')) for p in (ROOT / 'fixtures').glob('*.json')}

    def test_examples_cover_each_declared_action(self):
        actions=set(self.schema['properties']['action']['enum'])
        self.assertEqual(len(self.fixtures),len(actions))
        self.assertEqual({v['action'] for v in self.fixtures.values()},actions)
        for name, request in self.fixtures.items():
            with self.subTest(name=name):
                self.validator.validate(request)

    def test_unknown_fields_versions_and_actions_are_rejected(self):
        for key, value in [('silent_retry', True), ('api_version', 'v1'), ('action', 'uart.read')]:
            request = copy.deepcopy(self.fixtures['uart-write'])
            request[key] = value
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.validator.validate(request)

    def test_connection_revision_and_execution_budgets_are_required(self):
        for path in [('connection', 'revision'), ('timing', 'dispatch_within_ms'), ('timing', 'execution_timeout_ms')]:
            request = copy.deepcopy(self.fixtures['uart-write'])
            del request[path[0]][path[1]]
            with self.subTest(path=path), self.assertRaises(ValidationError):
                self.validator.validate(request)

    def test_action_parameters_are_not_interchangeable(self):
        request = copy.deepcopy(self.fixtures['target-flash'])
        request['params'] = self.fixtures['node-update']['params']
        with self.assertRaises(ValidationError):
            self.validator.validate(request)

    def test_invalid_bytes_addresses_and_mixed_i2c_messages_are_rejected(self):
        requests = []
        for data in ('a', '', 'zz'):
            request = copy.deepcopy(self.fixtures['uart-write'])
            request['params']['data_hex'] = data
            requests.append(request)
        for change in ({'address': 0}, {'address': 128}, {'messages': []},
                       {'messages': [{'write_hex': 'd0', 'read_length': 1}]}):
            request = copy.deepcopy(self.fixtures['i2c-register-read'])
            request['params'].update(change)
            requests.append(request)
        for index, request in enumerate(requests):
            with self.subTest(index=index), self.assertRaises(ValidationError):
                self.validator.validate(request)

    def test_strict_parser_rejects_ambiguous_numeric_and_duplicate_inputs(self):
        for raw in ('{"revision":1,"revision":2}', '{"timeout":NaN}', '{"timeout":Infinity}',
                    '{"nested":{"revision":1.0}}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                load(raw)
        request = copy.deepcopy(self.fixtures['uart-write'])
        request['connection']['revision'] = True
        with self.assertRaises(ValidationError):
            self.validator.validate(request)


if __name__ == '__main__':
    unittest.main(verbosity=2)
