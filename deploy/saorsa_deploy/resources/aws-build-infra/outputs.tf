output "bucket_name" {
  description = "Name of the S3 bucket for build artifacts"
  value       = aws_s3_bucket.builds.bucket
}

output "bucket_arn" {
  description = "ARN of the S3 bucket"
  value       = aws_s3_bucket.builds.arn
}

output "iam_user_name" {
  description = "Name of the IAM user for uploading builds"
  value       = aws_iam_user.build_uploader.name
}

output "iam_user_arn" {
  description = "ARN of the IAM user"
  value       = aws_iam_user.build_uploader.arn
}
