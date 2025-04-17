docker build --rm=true -f Dockerfile -t registry.peakit.pt/hummingbot . --pull=true --platform=linux/amd64
docker push registry.peakit.pt/hummingbot