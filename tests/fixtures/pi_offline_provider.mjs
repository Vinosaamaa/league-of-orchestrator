// Real Pi loads this extension in process; only the model backend is stubbed.
export default function registerLeagueOfflineProvider(pi) {
  const baseUrl = process.env.LEAGUE_TEST_PROVIDER_URL;
  if (!baseUrl) throw new Error("LEAGUE_TEST_PROVIDER_URL is required");
  pi.registerProvider("league-offline", {
    name: "League Offline Acceptance",
    baseUrl,
    apiKey: "$LEAGUE_TEST_PROVIDER_KEY",
    api: "openai-completions",
    models: [
      {
        id: "fixture-model",
        name: "Fixture Model",
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 16384,
        maxTokens: 1024,
      },
    ],
  });
}
