#!/bin/bash
# ai-log: push this cloud session's transcript, encrypted, to the branch
# transcripts/<cloud session id> of the session's repo, after every reply and
# at the end. cloud.py on the Mac fetches and decrypts it, because teleport
# returns only what followed the last compaction.
#
# Installed by ai-log's transcripts.py as .claude/ai-log/transcript.sh, next
# to transcripts.pem (the Mac's certificate; only its key can decrypt). Does
# nothing outside a cloud session. Never pushes anything unencrypted. Always
# exits 0, so it can't block a reply. Its log is /tmp/ai-log-transcript.log
# (or in $TMPDIR).

[ "$CLAUDE_CODE_REMOTE" = true ] || exit 0
run=${TMPDIR:-/tmp}
exec 2>>"$run/ai-log-transcript.log"
input=$(cat)
field() { printf '%s' "$input" | jq -r ".$1 // empty"; }

transcript=$(field transcript_path)
dir=${CLAUDE_PROJECT_DIR:-$(field cwd)}
id=${CLAUDE_CODE_REMOTE_SESSION_ID:-$(field session_id)}
cert="$dir/.claude/ai-log/transcripts.pem"
[ -f "$transcript" ] && [ -f "$cert" ] && [ -n "$id" ] || exit 0

# One push at a time: the Stop hook runs in the background and SessionEnd may overlap it.
exec 9>"$run/ai-log-transcript.lock"
flock -w 120 9 || exit 0

tmp=$(mktemp -d "$run/ai-log.XXXXXX") || exit 0
trap 'rm -rf "$tmp"' EXIT
name=$(basename "$transcript" .jsonl).jsonl.gz.cms
gzip -c "$transcript" > "$tmp/t.gz" &&
    openssl cms -encrypt -binary -aes-256-cbc -outform DER -in "$tmp/t.gz" -out "$tmp/$name" "$cert" || {
    echo "$(date -u +%FT%TZ) $id: couldn't encrypt; pushed nothing" >&2
    exit 0
}

# A commit with no parent holding just this file, made without touching the
# checkout; each push replaces the last, so GitHub keeps one copy.
export GIT_AUTHOR_NAME=ai-log GIT_AUTHOR_EMAIL=ai-log@localhost
export GIT_COMMITTER_NAME=ai-log GIT_COMMITTER_EMAIL=ai-log@localhost
cd "$dir" &&
    blob=$(git hash-object -w "$tmp/$name") &&
    tree=$(printf '100644 blob %s\t%s\n' "$blob" "$name" | git mktree) &&
    commit=$(git commit-tree "$tree" -m "ai-log transcript $id [skip ci]") &&
    git push -q --force origin "$commit:refs/heads/transcripts/$id" ||
    echo "$(date -u +%FT%TZ) $id: push failed" >&2
exit 0
