#!/bin/bash
# A deliberate refit, for a rating change or new history. Halts at the decision.
#
# fit.py reads data/magoun.parquet, which only build_dataset.py writes and nothing
# schedules -- so "refit" is the dataset rebuild too, not just calling fit.py.
#
# When the quantiles move, the 28 frozen fixtures break. A 30 s shift in the
# schedule offset fails all 28. That is the contract test working, and this script
# stops there on purpose: regenerating fixtures is a second, explicit run, because
# a fixture set regenerated to silence a failure is how the test quietly stops
# covering a tier. Read the row diff before you accept it.
#
#   ./src/refit.sh              rebuild the dataset, refit, diff, run the suite
#   ./src/refit.sh --fixtures   then, deliberately: regenerate and diff the rows
#
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PY="uv run --quiet --with polars --with numpy"
SUITE="uv run --quiet --with pytest --with numpy --with polars python -m pytest tests/ -q"
WORK="${TMPDIR:-/tmp}/refit-$$"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

if [ "${1:-}" = "--fixtures" ]; then
  echo "=== $(date -Iseconds) regenerating fixtures (deliberate) ==="
  cp -R tests/fixtures "$WORK/before"
  # Every archived day that already has a fixture file, so regeneration keeps the
  # same coverage instead of quietly narrowing to whatever is still on disk.
  days=$(ls tests/fixtures/cases-*.json | sed 's|.*cases-||; s|\.json||')
  echo "days: $(echo "$days" | tr '\n' ' ')"
  # shellcheck disable=SC2086
  $PY python src/make_fixtures.py $days || exit 1
  echo
  echo "--- what the refit did to the expected rows ---"
  $PY python src/refit.py fixtures "$WORK/before" tests/fixtures
  echo
  echo "--- suite against the regenerated fixtures ---"
  $SUITE || exit 1
  echo
  echo "Read the row diff above. If a tier stopped appearing, throw this away."
  echo "If it is right, commit the model and the fixtures TOGETHER:"
  echo "  git add data/model.json web/model.json web/model.js tests/fixtures"
  echo "  git commit"
  exit 0
fi

echo "=== $(date -Iseconds) refit ==="
cp data/model.json "$WORK/model-before.json"

echo "--- rebuilding data/magoun.parquet from data/raw ---"
$PY python src/build_dataset.py || exit 1

echo "--- fitting ---"
$PY python src/fit.py || exit 1

echo
echo "--- what moved ---"
$PY python src/refit.py model "$WORK/model-before.json" data/model.json

echo
echo "--- suite ---"
if $SUITE; then
  echo
  $PY python src/publish.py --check || exit 1
  echo
  echo "Green, and the model is unchanged enough that the fixtures still hold."
  echo "Publish it:  uv run python src/publish.py --commit"
  exit 0
fi

cat <<'MSG'

The suite is red. That is expected when the quantiles actually moved: the fixtures
freeze the old expected rows and a 30 s shift in the schedule offset fails all 28.

Do NOT regenerate fixtures to make this green. In order:
  1. Read the "what moved" diff above. If a tier was removed or the grid changed,
     stop -- that is a fitting bug, not a rating change.
  2. If the move is real, regenerate deliberately and read the row diff:
       ./src/refit.sh --fixtures
  3. Commit data/model.json, web/model.json, web/model.js and tests/fixtures in
     ONE commit, with what moved in the message.
MSG
exit 1
