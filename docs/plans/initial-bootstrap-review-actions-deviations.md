# Implementation deviations

## Credential concurrency regression: losing response cookie

The review plan says that the losing concurrent password-change response should
not receive a replacement session cookie. Litestar's
`ClientSideSessionBackend.store_in_message()` automatically serializes every
non-empty request session on every response, including the losing `422`
response, and generates a fresh encryption nonce. Consequently that response
does contain a `Set-Cookie` header, but it only reserializes the request's stale
session-version-0 state; it is not the winner's session-version-1 replacement.

The conservative implementation preserves the framework behavior rather than
clearing the losing response's session, because clearing it would also log a
user out after an ordinary incorrect-current-password submission. The
PostgreSQL regression instead asserts that the losing response does not receive
or retain the winner's replacement cookie and that its reserialized cookie
cannot access an authenticated endpoint after the winner commits.
