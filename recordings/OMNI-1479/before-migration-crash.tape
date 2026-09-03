Output recordings/OMNI-1479/before-migration-crash.mp4
Set FontSize 15
Set Width 1400
Set Height 800
Set Theme "Dracula"
Set TypingSpeed 30ms
Set Shell bash

Type "# OMNI-1479: PK-widening migration crashes on populated Postgres with a surviving FK"
Enter
Type "# A real PostgreSQL 16 is running locally; the scratch DB is stamped pre-r1a2b3c4d5e6,"
Enter
Type "# populated, and still holds host_permissions_user_id_fkey -> users.id."
Enter
Type "# The regression test drives the server boot path (alembic upgrade head):"
Enter
Sleep 1s
Type "./tmp-repro/run_test.sh"
Enter
Sleep 20s
Type ""
Sleep 5s
