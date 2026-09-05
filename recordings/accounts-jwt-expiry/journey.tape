Output recordings/accounts-jwt-expiry/after-accounts-jwt.mp4
Set FontSize 15
Set Width 1400
Set Height 800
Set TypingSpeed 25ms
Type "# OMNI fix: expired accounts JWT must not fall through to Databricks auth"
Enter
Type "python -m pytest tests/runner/test_accounts_jwt_expiry_fallthrough.py -q -p no:cacheprovider"
Enter
Wait+Screen@120s /3 passed/
Sleep 3s
