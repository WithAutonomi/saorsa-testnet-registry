variable "hcloud_token" {
  type      = string
  sensitive = true
}

variable "name" {
  type = string
}

variable "region" {
  type        = string
  description = "Hetzner location (e.g. fsn1, nbg1, hel1, ash)"
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
