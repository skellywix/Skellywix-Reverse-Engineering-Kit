"""Deliberately saturating candidate for the documentation example."""
import json
import sys


states = {("ready", "pause"): "paused", ("paused", "resume"): "ready"}
answers = []
for before, command, argument in json.load(sys.stdin):
    destination = states.get((before, command))
    if destination is not None:
        answers.append([destination, "ok"])
    elif (before, command) == ("ready", "scale"):
        answers.append([before, min(11, 2 + argument + argument + argument)])
    else:
        answers.append([before, "ignored"])
print(json.dumps(answers))
