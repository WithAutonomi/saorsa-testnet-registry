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
  description = "Client role: 'upload', 'download', 'wasm-upload', 'wasm-download' or 'pointer'"
  validation {
    # The wasm-* roles run a Node.js client driving ant-core compiled to WASM
    # over WebRTC-Direct. They are distinct roles rather than a flag on the
    # native ones because the role names the VM, its Terraform workspace and its
    # S3 state key -- telling the two transports apart is the point.
    # `pointer` drives `ant pointer create|update|get|resolve` for the life of
    # the run. Also its own role: a pointer client pays for every write from its
    # own wallet and publishes its own address list, so it needs its own VM name,
    # workspace and state key.
    condition     = contains(["upload", "download", "wasm-upload", "wasm-download", "pointer"], var.role)
    error_message = "role must be one of 'upload', 'download', 'wasm-upload', 'wasm-download', 'pointer'."
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
