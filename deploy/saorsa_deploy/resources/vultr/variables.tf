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

variable "ssh_key_ids" {
  type        = list(string)
  description = "IDs of pre-registered SSH keys in Vultr"
  default = [
    "4b172cfa-065c-4f0d-ad81-2e18ab81eef3", # saorsa-deploy
    "39c28fa0-fa9c-4cc3-9406-d0339b8128b6", # ant-production
  ]
}
