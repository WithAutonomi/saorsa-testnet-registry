terraform {
  required_version = ">= 1.0"

  required_providers {
    contabo = {
      source  = "contabo/contabo"
      version = ">= 0.1.42"
    }
  }

  backend "s3" {
    bucket = "maidsafe-org-infra-tfstate"
    region = "eu-west-2"
    # key is set at init time via -backend-config
  }
}
