variable "do_token" {
  type      = string
  sensitive = true
}

variable "name" {
  type        = string
  default     = "saorsa-registry-db"
  description = "Name of the managed Postgres cluster."
}

variable "region" {
  type        = string
  default     = "lon1"
  description = "DO region. Matches the app.yaml region."
}

variable "pg_version" {
  type        = string
  default     = "18"
  description = "Postgres major version. Matches the local source server (18.x) so the logical dump/restore needs no cross-version handling."
}

variable "size" {
  type        = string
  default     = "db-s-1vcpu-1gb"
  description = "Node size. Smallest available — adequate for the registry's modest data/traffic."
}

variable "node_count" {
  type        = number
  default     = 1
  description = "Number of nodes in the cluster (1 = no standby)."
}

variable "db_name" {
  type        = string
  default     = "saorsa_testnet_registry"
  description = "Application database name. Mirrors the local DB."
}

variable "db_user" {
  type        = string
  default     = "saorsa_testnet"
  description = "Application DB user handed to local tools + collaborators (not the cluster's doadmin superuser). Mirrors the local DB."
}
