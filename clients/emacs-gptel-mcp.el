;;; emacs-gptel-mcp.el --- Porter MCP server for Emacs (gptel via mcp.el)
;;
;; HOW TO APPLY: add the `setq' form below to your ~/.emacs.d/init.el
;; (requires mcp.el; for gptel-mcp use `gptel-mcp-servers' instead of
;; `mcp-hub-servers').  Replace {{PORTER_TOKEN}} with a real Porter
;; access token, then restart Emacs or re-evaluate the form.
;;
;; aidermacs users: Aider is already covered by aider-conf.yml; this file
;; is for gptel users.
;;
;; UNVERIFIED: not tested against a live Emacs install here.

(setq mcp-hub-servers
      '(("porter" :command "python"
         :args ("-m" "porter.mcp_stdio")
         :env (("PORTER_TOKEN" . "{{PORTER_TOKEN}}")
               ("PORTER_REPO" . "postgres")
               ("DATABASE_URL" . "postgresql://porter:porter@localhost:5432/porter")
               ("PORTER_JWT_SECRET" . "{{PORTER_JWT_SECRET}}")
               ("PORTER_EMBEDDER" . "http")
               ("EMBEDDER_URL" . "http://localhost:8001")
               ("PYTHONPATH" . "/path/to/porter")))))
