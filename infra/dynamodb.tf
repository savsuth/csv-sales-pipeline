# Single-key table: job_id (sha256 of bucket+key+versionId) is globally
# unique and is the only lookup pattern the handler needs. On-demand
# billing avoids needing to guess throughput for a workload that is,
# by nature, bursty and driven by upload volume.

resource "aws_dynamodb_table" "jobs" {
  name         = "${var.project_name}-jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}
