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

variable "ssh_key_ids" {
  type        = list(string)
  description = "IDs of pre-registered SSH keys in Hetzner"
  default = [
    "109855983", # ant-production
    "109945191", # ant-deploy
    "110444940", # testnet-key
  ]
}
