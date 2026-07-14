output "vm_ips" {
  description = "Public IPv4 addresses of the created instances, ordered by count.index."
  value       = [for inst in contabo_instance.node_vm : inst.ip_config[0].v4[0].ip]
}

output "vm_ids" {
  description = "Contabo instance IDs."
  value       = [for inst in contabo_instance.node_vm : inst.id]
}

output "volume_ids" {
  description = "Always empty — Contabo has no attachable block storage (root disk only)."
  value       = []
}
