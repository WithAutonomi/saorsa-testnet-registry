variable "hcloud_token" {
  type      = string
  sensitive = true
}

variable "name" {
  type = string
}

variable "region" {
  type        = string
  description = "Hetzner location (e.g. fsn1, nbg1, ash)"
}

variable "vm_count" {
  type = number
}

variable "attached_volume_size" {
  type    = number
  default = 20
}

variable "server_type" {
  type        = string
  default     = "cpx22"
  description = "Hetzner server type (e.g. cx22, cax21, cpx22)"
}

variable "name_template" {
  type        = string
  default     = "ant-node"
  description = <<EOT
Infix used between the deployment name and the region in resource names.
Defaults to "ant-node" (testnet style: <name>-ant-node-<region>-<i>).
Migration of existing prod fleets uses "hz" (e.g. ant-prod-01-hz-fsn1-1).
EOT
}

variable "ssh_key_ids" {
  type        = list(string)
  description = "IDs of pre-registered SSH keys in Hetzner"
  default = [
    "109855983", # ant-production
    "109945191", # ant-deploy
    "110444940", # testnet-key
  ]
}
