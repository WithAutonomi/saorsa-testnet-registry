output "iam_user_name" {
  description = "Name of the IAM user for client-VM S3 access"
  value       = aws_iam_user.client_s3.name
}

output "iam_user_arn" {
  description = "ARN of the IAM user"
  value       = aws_iam_user.client_s3.arn
}

output "access_key_id" {
  description = "AWS access key id for the client-VM IAM user. Copy into .env as ANT_CLIENT_AWS_ACCESS_KEY_ID."
  value       = aws_iam_access_key.client_s3.id
}

output "secret_access_key" {
  description = "AWS secret access key for the client-VM IAM user. Copy into .env as ANT_CLIENT_AWS_SECRET_ACCESS_KEY. Print with `terraform output -raw secret_access_key`."
  value       = aws_iam_access_key.client_s3.secret
  sensitive   = true
}

output "addresses_bucket" {
  description = "S3 bucket name the client creds are scoped to"
  value       = var.bucket_name
}

output "addresses_prefix" {
  description = "S3 key prefix the client creds are scoped to"
  value       = var.addresses_prefix
}
