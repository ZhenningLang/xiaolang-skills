"""Deterministic model/method/card assignment, independent of global RNG state."""

import random


def allocate(exam, config, seed):
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    rng = random.Random(seed)
    cards = sorted(card["id"] for card in exam["card_data"])
    models = sorted(config["models"])
    methods = sorted(config["methods"])
    rng.shuffle(methods)
    same = methods[0] if methods else None
    rows = []
    for index in range(config["agents"]):
        method = None
        if config["method_mode"] == "same":
            method = same
        elif config["method_mode"] == "different":
            method = methods[index % len(methods)]
        selected = []
        if config["materials_mode"] == "all":
            selected = cards[:]
        elif config["materials_mode"] == "proportion":
            # Floor, including zero cards; do not silently force one card per agent.
            selected = sorted(rng.sample(cards, int(len(cards) * config["materials_proportion"])))
        rows.append({"agent": index + 1, "model": rng.choice(models), "method": method,
                     "cards": selected})
    return rows
