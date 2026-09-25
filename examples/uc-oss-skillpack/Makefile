# Convenience targets for the UC OSS skillpack POC. Thin wrappers over run.sh
# so `make up` / `make down` work as expected.
#
# POC / not for production.

.PHONY: up down clean logs status demo

up:            ## Start the UC OSS server + bootstrap catalog/schema/volume
	./run.sh up

down:          ## Stop and remove the server (keeps ./data)
	./run.sh down

clean:         ## down + wipe ./data (deletes all stored packs)
	./run.sh clean

logs:          ## Tail the server logs
	./run.sh logs

status:        ## Show whether the REST API is reachable
	./run.sh status

demo:          ## Run the end-to-end demo (assumes `make up` already ran)
	./demo.sh
