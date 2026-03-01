variable "aws_region" {
  description = "AWS region for the S3 bucket and IAM resources"
  type        = string
  default     = "eu-west-2"
}

variable "bucket_name" {
  description = "Name of the S3 bucket for storing built binaries"
  type        = string
  default     = "saorsa-node-builds"
}
