"""Measure whether the think-compare skill helps: answers with the skill against plain asking, graded blind by a judge.

Three steps, each resumable (finished files are kept and skipped):

    python3 -m evals.compare.run answer --run-dir RUN --arm skill
    python3 -m evals.compare.run answer --run-dir RUN --arm plain
    python3 -m evals.compare.run judge --run-dir RUN --judge-cmd "<command>" --judge-label "<model>"
    python3 -m evals.compare.run summarize --run-dir RUN --out evals/compare/results/<date>-<name>.json

`answer` calls the Claude Code CLI (`claude -p`) with no tools, no settings, no MCP servers and no skills, from an
empty directory. The skill arm adds SKILL.md to the system prompt; the plain arm sends the question alone.

`judge` sends each pair of answers to a judge command twice, once in each order, with labels A and B only. The
command reads the prompt on stdin and prints the reply on stdout. Use a model from another company than the one
that answered.

`summarize` writes one result file with the questions, the prompts, every answer, every judge reply and the
counts, so each number can be traced to the text it came from.

Standard library only.
"""

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import random
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SKILL = ROOT / "skills" / "think-compare" / "SKILL.md"
QUESTIONS = HERE / "questions.json"
RUBRIC = HERE / "rubric.md"
CRITERIA = ("C1", "C2", "C3", "C4", "C5", "C6")
ARMS = ("skill", "plain")
ORDERS = ("first", "second")
SKILL_PREFACE = ("The user's request below matches the skill \"think-compare\". "
                 "Follow the skill's instructions when you answer.\n\n<skill>\n{skill}\n</skill>")


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_questions(path=QUESTIONS, only=None):
    questions = json.loads(Path(path).read_text(encoding="utf-8"))["questions"]
    if only:
        wanted = [q.strip() for q in only.split(",") if q.strip()]
        known = {q["id"] for q in questions}
        unknown = [q for q in wanted if q not in known]
        if unknown:
            raise SystemExit(f"unknown question ids: {', '.join(unknown)}")
        questions = [q for q in questions if q["id"] in wanted]
    return questions


# ---------- answers ----------

def claude_argv(executable, model, arm, skill_text):
    argv = [executable, "-p", "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
            "--tools", "", "--model", model, "--output-format", "json", "--no-session-persistence"]
    if arm == "skill":
        argv += ["--append-system-prompt", SKILL_PREFACE.format(skill=skill_text)]
    return argv


def call_claude(argv, prompt, timeout):
    with tempfile.TemporaryDirectory(prefix="compare-eval-") as cwd:
        done = subprocess.run(argv, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              cwd=cwd, timeout=timeout)
    try:
        payload = json.loads(done.stdout)
    except ValueError:
        raise RuntimeError(f"claude exit {done.returncode}, unreadable output: {done.stdout[:300]!r} "
                           f"stderr: {done.stderr[-300:]!r}")
    if done.returncode != 0 or payload.get("is_error") or not isinstance(payload.get("result"), str):
        raise RuntimeError(f"claude failed (exit {done.returncode}): {str(payload.get('result'))[:300]}")
    return payload


def answer_one(question, arm, args, skill_text, out):
    argv = claude_argv(args.claude, args.model, arm, skill_text)
    started = now()
    payload = call_claude(argv, question["text"], args.timeout)
    record = {
        "question_id": question["id"], "arm": arm, "model": args.model,
        "models_reported": sorted((payload.get("modelUsage") or {}).keys()),
        "skill_sha256": sha256(skill_text) if arm == "skill" else None,
        "prompt": question["text"], "answer": payload["result"].strip(),
        "cost_usd": payload.get("total_cost_usd"), "usage": payload.get("usage"),
        "started": started, "finished": now(),
    }
    write_json(out, record)
    return out


def cmd_answer(args):
    skill_text = Path(args.skill).read_text(encoding="utf-8")
    folder = Path(args.run_dir) / "answers"
    folder.mkdir(parents=True, exist_ok=True)
    todo = []
    for q in load_questions(args.questions_file, args.questions):
        out = folder / f"{q['id']}.{args.arm}.json"
        if out.exists() and not args.force:
            print(f"keep {out.name}")
            continue
        todo.append((q, out))
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(answer_one, q, args.arm, args, skill_text, out): out for q, out in todo}
        for future in concurrent.futures.as_completed(futures):
            try:
                print(f"wrote {future.result().name}", flush=True)
            except Exception as exc:  # report and go on; a rerun resumes
                failed += 1
                print(f"failed {futures[future].name}: {exc}", file=sys.stderr, flush=True)
    return 1 if failed else 0


# ---------- judging ----------

def first_order_skill_label(question_id, seed):
    """Which label the skill answer gets in the first order. The second order swaps them."""
    return random.Random(f"{seed}:{question_id}").choice(("A", "B"))


def mapping(question_id, seed, order):
    skill = first_order_skill_label(question_id, seed)
    if order == "second":
        skill = "B" if skill == "A" else "A"
    return {skill: "skill", ("B" if skill == "A" else "A"): "plain"}


def judge_prompt(rubric, question, answer_a, answer_b):
    return (f"{rubric.strip()}\n\n## Question\n\n{question.strip()}\n\n"
            f"## Answer A\n\n{answer_a.strip()}\n\n## Answer B\n\n{answer_b.strip()}\n")


def parse_judgment(reply):
    """Return the judge's JSON object, checked; raise ValueError when it is not usable."""
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the reply")
    data = json.loads(text[start:end + 1])
    for label in ("A", "B"):
        part = data.get(label)
        if not isinstance(part, dict):
            raise ValueError(f"missing scores for {label}")
        for c in CRITERIA:
            if part.get(c) not in (0, 1, 2) or isinstance(part.get(c), bool):
                raise ValueError(f"{label}.{c} is {part.get(c)!r}, not 0, 1 or 2")
    if data.get("preference") not in ("A", "B", "tie"):
        raise ValueError(f"preference is {data.get('preference')!r}")
    return data


def run_judge(command, prompt, timeout):
    done = subprocess.run(shlex.split(command), input=prompt, capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout)
    if done.returncode != 0:
        raise RuntimeError(f"judge exit {done.returncode}: {done.stderr[-300:]!r}")
    return done.stdout


def cmd_judge(args):
    run = Path(args.run_dir)
    folder = run / "judgments"
    folder.mkdir(parents=True, exist_ok=True)
    rubric = Path(args.rubric).read_text(encoding="utf-8")
    failed = 0
    for q in load_questions(args.questions_file, args.questions):
        answers = {arm: read_json(run / "answers" / f"{q['id']}.{arm}.json") for arm in ARMS}
        if None in answers.values():
            print(f"skip {q['id']}: an answer is missing", file=sys.stderr)
            failed += 1
            continue
        for order in ORDERS:
            out = folder / f"{q['id']}.{order}.json"
            if out.exists() and not args.force:
                print(f"keep {out.name}")
                continue
            labels = mapping(q["id"], args.seed, order)
            by_arm = {arm: label for label, arm in labels.items()}
            prompt = judge_prompt(rubric, q["text"], answers[labels["A"]]["answer"], answers[labels["B"]]["answer"])
            attempts = []
            parsed = None
            for _ in range(args.attempts):
                started = now()
                try:
                    reply = run_judge(args.judge_cmd, prompt, args.timeout)
                except Exception as exc:
                    attempts.append({"started": started, "error": str(exc)})
                    continue
                try:
                    parsed = parse_judgment(reply)
                    attempts.append({"started": started, "reply": reply})
                    break
                except ValueError as exc:
                    attempts.append({"started": started, "reply": reply, "error": str(exc)})
            if parsed is None:
                failed += 1
                print(f"failed {out.name}: {attempts[-1].get('error')}", file=sys.stderr, flush=True)
                write_json(folder / f"{q['id']}.{order}.failed.json", {"prompt": prompt, "attempts": attempts})
                continue
            write_json(out, {"question_id": q["id"], "order": order, "labels": labels,
                             "skill_label": by_arm["skill"], "judge": args.judge_label, "seed": args.seed,
                             "prompt_sha256": sha256(prompt), "attempts": attempts, "judgment": parsed})
            print(f"wrote {out.name}: preference {parsed['preference']} "
                  f"(skill is {by_arm['skill']})", flush=True)
    return 1 if failed else 0


# ---------- summary ----------

def total(scores):
    return sum(scores[c] for c in CRITERIA)


def summarize_question(judgments):
    """Per question: scores by arm in each order, preferred arm in each order, and whether both orders agree."""
    rows = []
    for j in judgments:
        labels = j["labels"]
        scores = {labels[label]: {c: j["judgment"][label][c] for c in CRITERIA} for label in ("A", "B")}
        pref = j["judgment"]["preference"]
        rows.append({
            "order": j["order"], "skill_label": j["skill_label"],
            "totals": {arm: total(scores[arm]) for arm in ARMS}, "scores": scores,
            "preferred": "tie" if pref == "tie" else labels[pref], "preferred_label": pref,
        })
    prefs = {r["preferred"] for r in rows}
    gaps = [r["totals"]["skill"] - r["totals"]["plain"] for r in rows]
    agree = len(rows) == 2 and len(prefs) == 1 and "tie" not in prefs
    score_direction = (len(rows) == 2 and (all(g > 0 for g in gaps) or all(g < 0 for g in gaps)))
    return {
        "orders": rows,
        "preferred_both_orders": prefs.pop() if agree else None,
        "score_gap_skill_minus_plain": gaps,
        "separates": bool(agree and score_direction and
                          (gaps[0] > 0) == (rows[0]["preferred"] == "skill")),
    }


def summarize(run, questions, label):
    per_question = {}
    all_rows = []
    for q in questions:
        judgments = [read_json(run / "judgments" / f"{q['id']}.{o}.json") for o in ORDERS]
        judgments = [j for j in judgments if j]
        if not judgments:
            continue
        per_question[q["id"]] = summarize_question(judgments)
        all_rows += per_question[q["id"]]["orders"]
    n = len(all_rows)
    means = {arm: {c: round(sum(r["scores"][arm][c] for r in all_rows) / n, 3) for c in CRITERIA} for arm in ARMS} \
        if n else {}
    for arm in means:
        means[arm]["total"] = round(sum(r["totals"][arm] for r in all_rows) / n, 3)
    counts = {
        "judgments": n,
        "preferred_skill": sum(r["preferred"] == "skill" for r in all_rows),
        "preferred_plain": sum(r["preferred"] == "plain" for r in all_rows),
        "tie": sum(r["preferred"] == "tie" for r in all_rows),
        "preferred_label_A": sum(r["preferred_label"] == "A" for r in all_rows),
        "preferred_label_B": sum(r["preferred_label"] == "B" for r in all_rows),
        "questions": len(per_question),
        "questions_skill_both_orders": sum(v["preferred_both_orders"] == "skill" for v in per_question.values()),
        "questions_plain_both_orders": sum(v["preferred_both_orders"] == "plain" for v in per_question.values()),
        "questions_split_or_tie": sum(v["preferred_both_orders"] is None for v in per_question.values()),
        "questions_separated": sum(v["separates"] for v in per_question.values()),
        "skill_total_higher": sum(r["totals"]["skill"] > r["totals"]["plain"] for r in all_rows),
        "plain_total_higher": sum(r["totals"]["skill"] < r["totals"]["plain"] for r in all_rows),
        "equal_total": sum(r["totals"]["skill"] == r["totals"]["plain"] for r in all_rows),
    }
    answers = {f"{q['id']}.{arm}": read_json(run / "answers" / f"{q['id']}.{arm}.json")
               for q in questions for arm in ARMS}
    judgments = {f"{q['id']}.{o}": read_json(run / "judgments" / f"{q['id']}.{o}.json")
                 for q in questions for o in ORDERS}
    skill_answers = [a for a in answers.values() if a and a["arm"] == "skill"]
    judges = sorted({j["judge"] for j in judgments.values() if j})
    rubric = RUBRIC.read_text(encoding="utf-8")
    return {
        "label": label,
        "date": datetime.date.today().isoformat(),
        "setup": {
            "answer_models": sorted({a["model"] for a in answers.values() if a}),
            "answer_models_reported": sorted({m for a in answers.values() if a for m in a["models_reported"]}),
            "judges": judges,
            "skill_sha256": sorted({a["skill_sha256"] for a in skill_answers}),
            "rubric_sha256": sha256(rubric),
            "orders": "each pair judged twice, labels A and B swapped",
            "answer_cost_usd": round(sum(a["cost_usd"] or 0 for a in answers.values() if a), 4),
        },
        "counts": counts,
        "mean_scores": means,
        "per_question": per_question,
        "questions": questions,
        "rubric": rubric,
        "answers": answers,
        "judgments": judgments,
    }


def cmd_summarize(args):
    run = Path(args.run_dir)
    data = summarize(run, load_questions(args.questions_file, args.questions), args.label)
    write_json(Path(args.out), data)
    c, m = data["counts"], data["mean_scores"]
    print(json.dumps({"counts": c, "mean_total": {arm: m[arm]["total"] for arm in m}}, indent=2))
    print(f"wrote {args.out}")
    return 0


# ---------- helpers ----------

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m evals.compare.run", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--run-dir", required=True, help="where answers and judgments are kept")
    common.add_argument("--questions", help="comma-separated question ids; default all")
    common.add_argument("--questions-file", default=str(QUESTIONS))

    a = sub.add_parser("answer", parents=[common], help="answer every question in one arm")
    a.add_argument("--arm", choices=ARMS, required=True)
    a.add_argument("--model", default="claude-opus-5-5")
    a.add_argument("--claude", default="claude", help="the Claude Code executable")
    a.add_argument("--skill", default=str(SKILL))
    a.add_argument("--jobs", type=int, default=1)
    a.add_argument("--timeout", type=int, default=900)
    a.add_argument("--force", action="store_true", help="answer again even if an answer file exists")

    j = sub.add_parser("judge", parents=[common], help="grade each pair twice, once in each order")
    j.add_argument("--judge-cmd", required=True, help="reads the prompt on stdin, prints the reply")
    j.add_argument("--judge-label", required=True, help="the judge model, written into each judgment")
    j.add_argument("--rubric", default=str(RUBRIC))
    j.add_argument("--seed", default="1")
    j.add_argument("--attempts", type=int, default=2)
    j.add_argument("--timeout", type=int, default=900)
    j.add_argument("--force", action="store_true")

    s = sub.add_parser("summarize", parents=[common], help="write the result file")
    s.add_argument("--out", required=True)
    s.add_argument("--label", required=True, help="what this run is, written into the result file")

    args = parser.parse_args(argv)
    return {"answer": cmd_answer, "judge": cmd_judge, "summarize": cmd_summarize}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
