terraform {
  required_version = ">= 1.0"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.0"
    }
  }

  # Standalone, one-off application infrastructure (like aws-client-s3): a fixed
  # state key, applied manually with `terraform -chdir=... apply`. NOT part of the
  # per-deployment lifecycle modules whose key is set at init via -backend-config.
  backend "s3" {
    bucket = "maidsafe-org-infra-tfstate"
    region = "eu-west-2"
    key    = "saorsa-deploy/digitalocean-registry-db.tfstate"
  }
}
