import { api, ApiError, onUnauthenticated, setAccessToken } from "../api/client";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("api client", () => {
  afterEach(() => { vi.restoreAllMocks(); setAccessToken(null); });

  it("sends the bearer token and parses JSON", async () => {
    setAccessToken("abc");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(json(200, { ok: true }));
    await expect(api("/thing")).resolves.toEqual({ ok: true });
    const init = fetchMock.mock.calls[0]![1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer abc");
  });

  it("refreshes once on 401 and retries with the new token", async () => {
    setAccessToken("expired");
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json(401, { detail: "expired" }))
      .mockResolvedValueOnce(json(200, { access_token: "fresh" }))
      .mockResolvedValueOnce(json(200, { value: 1 }));
    await expect(api("/thing")).resolves.toEqual({ value: 1 });
    expect(fetchMock.mock.calls[1]![0]).toBe("/api/v1/auth/refresh");
    const retry = fetchMock.mock.calls[2]![1] as RequestInit;
    expect((retry.headers as Record<string, string>).Authorization).toBe("Bearer fresh");
  });

  it("signals logout when refresh fails", async () => {
    const lost = vi.fn();
    onUnauthenticated(lost);
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json(401, { detail: "expired" }))
      .mockResolvedValueOnce(json(401, { detail: "no refresh" }));
    await expect(api("/thing")).rejects.toBeInstanceOf(ApiError);
    expect(lost).toHaveBeenCalled();
  });

  it("turns problem-details into ApiError with field errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      json(422, { title: "Validation error", detail: "Request body or parameters are invalid", errors: [{ msg: "bad" }] }));
    const err = (await api("/thing", { body: {} }).catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(422);
    expect(err.errors[0]?.msg).toBe("bad");
  });
});
