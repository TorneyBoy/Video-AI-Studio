// Set only after the HTTPS account service and the WeChat domain allowlists
// are ready. The current huayuan.ink DNS still belongs to the old service.
// AppSecret and supplier credentials never belong in this client project.
export const clientConfig = {
  apiBase: '',
  requestTimeoutMs: 20000,
};
