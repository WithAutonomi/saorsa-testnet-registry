output "vm_ips" {
  description = "Public IPv4 addresses of the created instances, ordered by count.index."
  value       = [for inst in openstack_compute_instance_v2.node_vm : inst.access_ip_v4]
}

output "vm_ids" {
  description = "OpenStack/Nova instance IDs."
  value       = [for inst in openstack_compute_instance_v2.node_vm : inst.id]
}

output "volume_ids" {
  description = "Always empty — 3-AZ regions have no block storage (root disk only)."
  value       = []
}
