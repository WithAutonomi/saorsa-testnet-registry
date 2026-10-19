variable "do_token" {
  type      = string
  sensitive = true
}

variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "role" {
  type        = string
  description = "Client role: 'upload', 'download', 'wasm-upload', 'wasm-download', 'pointer' or 'browser-download'"
  validation {
    # The wasm-* roles run a Node.js client driving ant-core compiled to WASM
    # over WebRTC-Direct. They are distinct roles rather than a flag on the
    # native ones because the role names the VM, its Terraform workspace and its
    # S3 state key -- telling the two transports apart is the point.
    # `pointer` drives `ant pointer create|update|get|resolve` for the life of
    # the run. Also its own role: a pointer client pays for every write from its
    # own wallet and publishes its own address list, so it needs its own VM name,
    # workspace and state key.
    # `browser-download` runs N headless Chromium instances through the harness
    # in `resources/browser-client/`, all reading one chosen hot file on a shared
    # wall-clock schedule, and takes no wallet. Its own role for the same reason
    # again -- the role names the VM, its Terraform workspace and its S3 state
    # key -- and because `clients clean` and `clients destroy` dispatch on it.
    condition     = contains(["upload", "download", "wasm-upload", "wasm-download", "pointer", "browser-download"], var.role)
    error_message = "role must be one of 'upload', 'download', 'wasm-upload', 'wasm-download', 'pointer', 'browser-download'."
  }
}

variable "client_index" {
  type        = number
  description = "Index of this client VM (used in naming)"
}

variable "droplet_size" {
  type        = string
  default     = "s-2vcpu-4gb"
  description = "Droplet size slug (e.g. s-2vcpu-4gb, s-4vcpu-8gb)"
}

variable "ssh_key_ids" {
  type = list(number)
  default = [
    36971688, # David Irvine
    30643816, # Anselme Grumbach
    30113222, # Qi Ma
    42022675, # Shu
    30878672, # Chris O'Neil
    31216015, # QA
    34183228, # GH Actions Automation
    38596814, # sn-testnet-workflows automation
    54385801  # saorsa-deploy
  ]
}
