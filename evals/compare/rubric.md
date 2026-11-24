You are grading two answers to the same technology-selection question. Someone has to choose between the candidates and then defend the choice to their team. Judge which answer helps them more.

Score each answer on its own against the six criteria below, 0, 1 or 2 each. Then say which answer you prefer overall.

Rules for grading:

- Do not reward length. A shorter answer that does the job scores as well as a longer one.
- Do not reward formatting. A table, headings or labels earn nothing by themselves; judge what the answer says.
- The order of the two answers means nothing.
- Grade with your own knowledge of the candidates. A factual error costs points under C6 even when it sounds confident.

Criteria:

- C1 Criteria first. 2: names the criteria this choice turns on, tied to the situation described, and compares on them. 1: some criteria, but generic or mixed with ones that do not separate the candidates. 0: a loose list of pros and cons, or a verdict with little comparison.
- C2 Concrete comparison. 2: for each criterion, states differences between the candidates that someone could check (named features, limits, operational work, rough numbers). 1: partly concrete, partly vague adjectives such as "more flexible" or "better ecosystem". 0: mostly vague.
- C3 Uses the situation. 2: uses the facts given in the question (team skills, scale, budget, operations capacity, stated needs) and lets the hard constraints outweigh minor preferences. 1: mentions the facts but the comparison does not really depend on them. 0: a generic answer that would read the same for any team.
- C4 Clear verdict. 2: ends with one recommendation, or an explicit tie with a safe default and what would break the tie. 1: a lean, but hedged until it is hard to act on. 0: "it depends" with no choice.
- C5 Conditions. 2: says what the recommendation relies on and names when another candidate would be the better choice. 1: a vague caveat. 0: none.
- C6 Accuracy and honesty. 2: no factual errors about the candidates, and claims that are uncertain or rest on facts not given are marked as such. 1: a minor error, or uncertain claims stated as certain. 0: an error that could change the decision.

Reply with one JSON object and nothing else, in this shape:

{"A": {"C1": 0, "C2": 0, "C3": 0, "C4": 0, "C5": 0, "C6": 0, "notes": "one or two sentences"},
 "B": {"C1": 0, "C2": 0, "C3": 0, "C4": 0, "C5": 0, "C6": 0, "notes": "one or two sentences"},
 "preference": "A" or "B" or "tie",
 "reason": "one or two sentences on why"}
