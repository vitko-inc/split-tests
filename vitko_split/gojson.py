"""Run ``go test -json``, keep its events in a file and print its normal output.

usage: python -m vitko_split.gojson <events file> -- go test ... -json ..."""

import json
import subprocess
import sys


def main(argv):
    events_path, argv = argv[0], argv[2:]
    with open(events_path, "w") as events:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE)
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace")
            events.write(line)
            try:
                event = json.loads(line)
            except ValueError:
                sys.stdout.write(line)  # build errors and anything else that isn't an event
            else:
                if event.get("Action") == "output":
                    sys.stdout.write(event.get("Output", ""))
            sys.stdout.flush()
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
