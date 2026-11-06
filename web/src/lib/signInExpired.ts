/**
 * Error code for a request answered by a sign-in page instead of the API. Kept in its
 * own module so tests that mock `identity` do not drop it.
 */
export const SIGN_IN_EXPIRED_CODE = "sign_in_expired";
