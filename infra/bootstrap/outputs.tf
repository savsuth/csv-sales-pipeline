output "state_bucket_name" {
  description = "Put this in infra/backend.hcl as `bucket`."
  value       = aws_s3_bucket.state.id
}

output "backend_hcl" {
  description = "Ready-to-save contents for infra/backend.hcl."
  value       = <<-EOT
    bucket = "${aws_s3_bucket.state.id}"
    key    = "${var.state_key}"
    region = "${var.aws_region}"
  EOT
}

output "github_actions_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN Actions variable. Null unless enable_github_oidc = true."
  value       = var.enable_github_oidc ? aws_iam_role.github_actions_deploy[0].arn : null
}
