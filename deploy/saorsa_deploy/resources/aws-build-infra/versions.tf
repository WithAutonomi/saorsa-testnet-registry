terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket = "maidsafe-org-infra-tfstate"
    region = "eu-west-2"
    key    = "saorsa-deploy/aws-build-infra.tfstate"
  }
}
