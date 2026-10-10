"""Keep vendor protobuf descriptors out of Home Assistant's global pool."""
from google.protobuf.descriptor_pool import DescriptorPool
pool = DescriptorPool()
