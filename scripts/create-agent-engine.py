"""Finds or creates the Agent Engine instance that holds an environment's code-execution sandboxes.

Idempotent: an instance with the same display name is reused. Prints the resource name to put in
AGENT_ENGINE_NAME. Needs `roles/aiplatform.user` (or more) and the aiplatform API enabled.

    .venv\\Scripts\\python.exe scripts\\create-agent-engine.py --project agentfastapi-dev-nv01 --display-name agentfastapi-dev

The first sandbox on a new instance takes about two minutes (one-time setup); later ones take ~1 s.
"""

import argparse
import sys

import agentplatform


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True, help="GCP project id")
    parser.add_argument("--location", default="asia-south1")
    parser.add_argument("--display-name", required=True, help="e.g. agentfastapi-local, agentfastapi-dev")
    args = parser.parse_args()

    client = agentplatform.Client(project=args.project, location=args.location)
    matches = [r for r in client.runtimes.list() if (r.api_resource.display_name or "") == args.display_name]
    if len(matches) > 1:
        names = ", ".join(r.api_resource.name for r in matches)
        print(f"More than one instance is named {args.display_name!r}: {names}", file=sys.stderr)
        return 1
    if matches:
        name = matches[0].api_resource.name
        print(f"Reusing {name}", file=sys.stderr)
    else:
        name = client.runtimes.create(config={"display_name": args.display_name}).api_resource.name
        print(f"Created {name}", file=sys.stderr)
    print(f"AGENT_ENGINE_NAME={name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
