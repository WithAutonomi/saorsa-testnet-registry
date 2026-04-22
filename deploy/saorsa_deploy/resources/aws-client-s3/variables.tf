variable "aws_region" {
  description = "AWS region for the IAM resources (bucket is already regional)"
  type        = string
  default     = "eu-west-2"
}

variable "bucket_name" {
  description = "Name of the S3 bucket that holds the addresses folder"
  type        = string
  default     = "maidsafe-org-infra-tfstate"
}

variable "addresses_prefix" {
  description = "S3 key prefix under which per-testnet address folders live. The IAM user gets read/write/delete on this prefix only."
  type        = string
  default     = "saorsa-deploy/addresses"
}

variable "iam_user_name" {
  description = "Name of the IAM user that client VMs use for S3 access"
  type        = string
  default     = "saorsa-deploy-client-s3"
}
