# A CLI that answers SIGTERM by exiting 7 instead of dying by the signal.
# The sleep runs in the background so `wait` returns as soon as the trap fires;
# the trap reaps it before exiting so no zombie outlives the fixture.
trap 'kill "$sleeper" 2>/dev/null; wait "$sleeper" 2>/dev/null; exit 7' TERM
sleep 20 &
sleeper=$!
wait "$sleeper"
