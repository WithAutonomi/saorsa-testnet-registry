output "vm_ip" {
  description = "Public IPv4 address of the created instance."
  # OVH sometimes attaches a SECOND public IPv4 to an instance. `one()` raises
  # "Invalid function argument" on a 2-element list, which broke `apply` AND
  # `destroy` — destroy evaluates outputs too, so the region became
  # undestroyable and had to be cleared through the OVH API by hand
  # (V2-1424 / DEV-06 DE1, 2026-10-05). `addresses` is a set, so iteration
  # order is not guaranteed: sort first, then take the lowest address, so the
  # value is stable across runs. `try(..., null)` keeps `one()`'s old
  # empty-list result and guarantees this output can never fail a destroy.
  value = try(sort([
    for a in ovh_cloud_project_instance.client_vm.addresses : a.ip
    if a.version == 4
  ])[0], null)
}

output "vm_id" {
  description = "OVH/OpenStack instance ID."
  value       = ovh_cloud_project_instance.client_vm.id
}
