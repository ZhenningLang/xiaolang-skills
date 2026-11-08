"""The durable record of which failed unit each new repetition stands in for. Every unit reader goes through here."""

from pathlib import Path

from .budget import atomic_write, dumps
from .exam import read_jsonl


NAME = "replacements.jsonl"
FIELDS = {"exam_id", "config", "failed_rep", "new_rep", "stage", "cause", "call_keys", "ungrouped_ideas", "reason", "ts"}


def record_path(run_dir, exam_id):
    return Path(run_dir).resolve() / exam_id / NAME


def load(run_dir, exam_id):
    """A broken record is an error: guessing here could count a failed unit or hide a replacement."""
    path = record_path(run_dir, exam_id)
    rows = read_jsonl(path) if path.exists() else []
    failed, new = set(), set()
    for row in rows:
        if set(row) != FIELDS or row["exam_id"] != exam_id:
            raise ValueError(f"Invalid replacement record for {exam_id}: unexpected fields")
        before, after = (row["config"], row["failed_rep"]), (row["config"], row["new_rep"])
        if row["new_rep"] is None:
            # Abandoned: only a replacement that also failed, and only once; nothing stands in for it.
            if before not in new or before in failed:
                raise ValueError(f"Invalid replacement record for {exam_id}: {row['config']} rep {row['failed_rep']} "
                                 "is abandoned but is not a replacement, or is abandoned twice")
            failed.add(before)
            continue
        if before in failed or before in new or after in failed or after in new or before == after:
            raise ValueError(f"Invalid replacement record for {exam_id}: {row['config']} rep {row['failed_rep']} "
                             "appears in more than one replacement")
        failed.add(before)
        new.add(after)
    return rows


def append(run_dir, exam_id, row):
    path = record_path(run_dir, exam_id)
    prefix = path.read_text(encoding="utf-8") if path.exists() else ""
    atomic_write(path, prefix + dumps(row) + "\n")


def retired_reps(rows, config):
    return {row["failed_rep"] for row in rows if row["config"] == config}


def new_reps(rows, config):
    return {row["new_rep"] for row in rows if row["config"] == config and row["new_rep"] is not None}


def abandoned_reps(rows, config):
    return {row["failed_rep"] for row in rows if row["config"] == config and row["new_rep"] is None}


def slot(rows, config, rep):
    """The repetition a unit counts as: a replacement takes the place of the unit it replaced."""
    return next((row["failed_rep"] for row in rows if row["config"] == config and row["new_rep"] == rep), rep)


def refuse_if_retired(run_dir, exam_id, config, rep):
    rows = load(run_dir, exam_id)
    if rep in abandoned_reps(rows, config):
        raise ValueError(f"{exam_id}/{config} rep {rep} was abandoned; it keeps its files and cost but cannot run again")
    if rep in retired_reps(rows, config):
        raise ValueError(f"{exam_id}/{config} rep {rep} was replaced; it keeps its files and cost but cannot run again")
