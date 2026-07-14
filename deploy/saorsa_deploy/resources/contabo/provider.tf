# The Contabo provider authenticates via an OAuth2 password flow, reading all
# four credentials straight from the environment:
#   CNTB_OAUTH2_CLIENT_ID / CNTB_OAUTH2_CLIENT_SECRET / CNTB_OAUTH2_USER / CNTB_OAUTH2_PASS
# (CNTB_OAUTH2_USER is the account email; CNTB_OAUTH2_PASS is the separate API
# password set under the account security page, NOT the login password.)
# The saorsa-deploy CLI validates these env vars before invoking terraform, so
# no arguments are needed here.
provider "contabo" {}
