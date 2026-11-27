"""Original toy for the documentation example. No external inputs besides JSON stdin."""
import json
import sys


def observe(state, action, value):
    if state == "ready":
        if action == "pause":
            return "paused", "ok"
        if action == "scale":
            return "ready", 3 * value + 2
    if state == "paused" and action == "resume":
        return "ready", "ok"
    return state, "ignored"


print(json.dumps([observe(*case) for case in json.load(sys.stdin)]))
