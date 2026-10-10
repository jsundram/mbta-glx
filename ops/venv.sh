# Sourced, not run: where the agents' own Python environment lives.
#
# The agents used to start with `uv run --with ...`, and a `uv run` parent stays
# alive for as long as its child does, holding a shared lock on ~/.cache/uv/.lock.
# Three agents under KeepAlive held it for weeks, so `uv cache clean` / `prune`
# waited forever -- and `--force` would have deleted the packages they import,
# since an ephemeral `--with` environment reads them straight out of the cache.
#
# So the agents run this environment's python directly and no uv process outlives
# install.sh. It is built from ops/requirements.txt with --link-mode clone: on APFS
# a clone is an independent copy-on-write file, so clearing the cache cannot reach
# it. It lives outside the repo because the repo is in Dropbox, which would sync
# every file of polars and numpy.
MAGOUN_VENV="${MAGOUN_VENV:-$HOME/.local/share/magoun/venv}"
PY="$MAGOUN_VENV/bin/python"
