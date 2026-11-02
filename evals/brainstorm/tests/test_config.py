import unittest

from evals.brainstorm.config import validate_config


class ConfigTests(unittest.TestCase):
    def test_invalid_backend_options_and_model_prices_rejected_at_validation(self):
        for backend in ({"kind": "fake", "usd": -1}, {"kind": "fake", "reserve_usd": 0},
                        {"kind": "fake", "unsupported": 1}, {"kind": "fake", "responses": "not_an_array"},
                        {"kind": "claude", "pricing": {}}, {"kind": "command"}):
            with self.subTest(backend=backend), self.assertRaises(ValueError):
                validate_config({"name": "test", "default_model": "m", "backend": backend})

    def test_all_switches_validated_and_preserved(self):
        fields = {"method_mode": "different", "methods": {"reverse": "Test reverse"},
                  "materials_mode": "proportion", "materials_proportion": .25,
                  "three_step": True, "predict_peers": True, "serial_visible": True,
                  "return_collisions": True, "before_generation": True,
                  "decomposition_timing": "late", "levels": ["round2", "pressure4"],
                  "stop_on_exhaustion": True, "max_batches": 4, "agents": 2,
                  "max_output_tokens": 99, "models": ["m", "other"]}
        actual = validate_config({"name": "test", "default_model": "m", **fields})
        for key, value in fields.items():
            self.assertEqual(actual[key], value)
        for key, value in (("agents", True), ("max_batches", 0), ("max_output_tokens", -1),
                           ("three_step", "true"), ("predict_peers", 1), ("serial_visible", None),
                           ("return_collisions", "false"), ("before_generation", 1),
                           ("stop_on_exhaustion", []), ("materials_mode", "any"),
                           ("materials_proportion", 1.1), ("materials_proportion", True),
                           ("decomposition_timing", "sometime"), ("levels", ["pressure5"]),
                           ("models", []), ("methods", {"bad": ""}), ("budget_usd", "NaN"),
                           ("name", "../escape"), ("unknown", True)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_config({"name": "test", "default_model": "m", key: value})

    def test_default_model_is_the_only_implicitly_enabled_model(self):
        config = validate_config({"name": "test", "default_model": "test_model"})
        self.assertEqual(config["models"], ["test_model"])
        self.assertEqual(config["budget_usd"], "50")
        self.assertEqual(config["backend"], {"kind": "fake"})
        self.assertEqual(config["agents"], 3)
