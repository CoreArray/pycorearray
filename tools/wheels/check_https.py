"""Read a public GDS file on Amazon S3 over HTTPS, which checks that libcurl
verifies the server with the CA bundle found on this system"""

import pycorearray as gds

print('CA bundle:', repr(gds.cloud.options()['ca_bundle']))
f = gds.cloud.open('s3://gds-stat/download/hapmap/hapmap_r23a.gds')
v = f.index('position').read(start=[1000], count=[5])
f.close()
assert list(v) == [2409958, 2410723, 2410773, 2412474, 2413426], v
print('HTTPS: OK')
