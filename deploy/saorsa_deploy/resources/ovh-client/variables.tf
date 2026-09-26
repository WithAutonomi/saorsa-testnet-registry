variable "service_name" {
  type        = string
  description = "OVH Public Cloud project ID (UUID-shaped string)."
}

variable "name" {
  type = string
}

variable "region" {
  type        = string
  description = "OVH region (e.g. BHS5, GRA9, DE1, UK1, WAW1)."
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

variable "flavor_name" {
  type        = string
  default     = "b3-8"
  description = "OVH flavor name (e.g. b3-8 for 2 vCPU / 8 GB, b3-16 for 4 vCPU / 16 GB)."
}

variable "ssh_key_name" {
  type        = string
  default     = "ant_deploy"
  description = "Name of the SSH key already registered in the OVH project."
}

variable "ubuntu_image_name_pattern" {
  type        = string
  default     = "Ubuntu 24.04"
  description = "Substring matched against image names to pick the Ubuntu image."
}
