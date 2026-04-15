variable "vultr_api_key" {
  type      = string
  sensitive = true
}

variable "name" {
  type = string
}

variable "region" {
  type        = string
  description = "Vultr region ID (e.g. ewr, lhr, fra)"
}

variable "vm_count" {
  type = number
}

variable "attached_volume_size" {
  type    = number
  default = 20
}

variable "plan" {
  type        = string
  default     = "vc2-2c-4gb"
  description = "Vultr plan slug (e.g. vc2-2c-4gb, vc2-4c-8gb)"
}

variable "ssh_key_ids" {
  type        = list(string)
  description = "IDs of pre-registered SSH keys in Vultr"
  default = [
    "4b172cfa-065c-4f0d-ad81-2e18ab81eef3", # saorsa-deploy
    "39c28fa0-fa9c-4cc3-9406-d0339b8128b6", # ant-production
  ]
}
