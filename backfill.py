name: X backfill

# Manual only. This walks BACKWARD through the timeline to build archive.json,
# the permanent research history, and it is deliberately not on a schedule:
# it burns API quota and only needs to run until the archive reaches the
# timeline's 3,200-post floor.
#
# Run it from the Actions tab with "Run workflow". Start with the default 10
# pages (~1,000 posts). If it reports "more", run it again — each run resumes
# from the saved cursor. When it prints "END OF TIMELINE" the archive is done
# and further runs are no-ops.

on:
  workflow_dispatch:
    inputs:
      pages:
        description: "Pages to fetch this run (100 posts per page)"
        required: false
        default: "10"

permissions:
  contents: write

concurrency:
  group: x-backfill
  cancel-in-progress: false

jobs:
  backfill:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Fetch history
        env:
          X_BEARER_TOKEN: ${{ secrets.X_BEARER_TOKEN }}
          X_ACCOUNT: FL0WG0D
          BACKFILL_PAGES: ${{ github.event.inputs.pages }}
        run: python backfill.py

      - name: Commit
        run: |
          git config user.name "x-backfill"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add -A
          if git diff --staged --quiet; then
            echo "nothing new"
          else
            git commit -m "backfill: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
            # The scheduled relay commits to the same branch, so rebase rather
            # than fail if it landed a commit while this job was running.
            git pull --rebase origin main
            git push
          fi
