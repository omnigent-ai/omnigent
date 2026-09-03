Output recordings/OMNI-1479/after-migration-crash.mp4
Set FontSize 15
Set Width 1400
Set Height 800
Set Theme "Dracula"
Set TypingSpeed 30ms
Set Shell bash

Type "# FIXED: PK-widening migration now survives a populated Postgres with a surviving FK"
Enter
Type "# Same setup: real PostgreSQL 16, scratch DB stamped pre-r1a2b3c4d5e6, populated,"
Enter
Type "# still holding host_permissions_user_id_fkey -> users.id."
Enter
Type "# The migration re-binds the FK onto a parallel UNIQUE(id) instead of dropping it:"
Enter
Sleep 1s
Type "./tmp-repro/run_test_after.sh"
Enter
Sleep 25s
Type ""
Sleep 5s
