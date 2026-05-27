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

variable "role" {
  type        = string
  description = "Client role: 'upload' or 'download'"
  validation {
    condition     = contains(["upload", "download"], var.role)
    error_message = "role must be either 'upload' or 'download'."
  }
}

variable "client_index" {
  type        = number
  description = "Index of this client VM (used in naming)"
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
