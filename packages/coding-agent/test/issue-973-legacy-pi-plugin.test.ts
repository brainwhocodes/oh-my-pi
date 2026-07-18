import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { loadExtensions } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/loader";
import { TempDir } from "@oh-my-pi/pi-utils";

const currentPiCodingAgentPath = Bun.resolveSync("@oh-my-pi/pi-coding-agent", import.meta.dir);
const currentPiExtensionsPath = Bun.resolveSync("@oh-my-pi/pi-coding-agent/extensibility/extensions", import.meta.dir);

describe("issue #973: legacy Pi plugin imports", () => {
	let projectDir: TempDir;
	let extensionPath: string;
	let imageExtensionPath: string;

	beforeEach(() => {
		projectDir = TempDir.createSync("@issue-973-");
		const pluginDir = path.join(projectDir.path(), "legacy-pi-plugin");
		extensionPath = path.join(pluginDir, "dist", "extension.ts");
		fs.mkdirSync(path.dirname(extensionPath), { recursive: true });
		fs.writeFileSync(
			path.join(pluginDir, "package.json"),
			JSON.stringify({
				name: "legacy-pi-plugin",
				version: "1.0.0",
				pi: {
					extensions: ["./dist/extension.ts"],
				},
			}),
		);
		fs.writeFileSync(
			extensionPath,
			[
				'import { isToolCallEventType as legacyRoot } from "@mariozechner/pi-coding-agent";',
				'import { isToolCallEventType as legacyExtensions } from "@mariozechner/pi-coding-agent/extensibility/extensions";',
				`import { isToolCallEventType as modernRoot } from ${JSON.stringify(currentPiCodingAgentPath)};`,
				`import { isToolCallEventType as modernExtensions } from ${JSON.stringify(currentPiExtensionsPath)};`,
				"",
				'if (legacyRoot !== modernRoot) throw new Error("legacy root import did not remap");',
				'if (legacyExtensions !== modernExtensions) throw new Error("legacy extension import did not remap");',
				"",
				"export default function(pi) {",
				'\tpi.registerCommand("legacy-pi-ext", { handler: async () => {} });',
				"}",
			].join("\n"),
		);

		const imagePluginDir = path.join(projectDir.path(), "pi-codex-image-gen");
		imageExtensionPath = path.join(imagePluginDir, "dist", "extension.ts");
		fs.mkdirSync(path.dirname(imageExtensionPath), { recursive: true });
		fs.writeFileSync(
			path.join(imagePluginDir, "package.json"),
			JSON.stringify({
				name: "pi-codex-image-gen",
				version: "0.1.11",
				pi: {
					extensions: ["./dist/extension.ts"],
				},
			}),
		);
		fs.writeFileSync(
			imageExtensionPath,
			[
				'import * as path from "node:path";',
				'import { StringEnum } from "@earendil-works/pi-ai";',
				'import { getAgentDir, withFileMutationQueue } from "@earendil-works/pi-coding-agent";',
				'import { Type } from "typebox";',
				"",
				"const parameters = Type.Object({",
				'\tprompt: Type.String({ description: "Image prompt" }),',
				'\tmode: StringEnum(["generate"] as const),',
				"});",
				"",
				"export default function(pi) {",
				"\tpi.registerTool({",
				'\t\tname: "codex_generate_image",',
				'\t\tlabel: "Generate image",',
				'\t\tdescription: "Plugin-shaped compatibility probe",',
				"\t\tparameters,",
				"\t\texecute: async (toolCallId) => {",
				"\t\t\tconst events = [];",
				'\t\t\tconst pathA = path.join(getAgentDir(), "codex-image-gen-a-" + toolCallId);',
				'\t\t\tconst pathB = path.join(getAgentDir(), "codex-image-gen-b-" + toolCallId);',
				"\t\t\tconst firstAStarted = Promise.withResolvers();",
				"\t\t\tconst releaseFirstA = Promise.withResolvers();",
				"\t\t\tconst firstA = withFileMutationQueue(pathA, async () => {",
				'\t\t\t\tevents.push("a1-start");',
				"\t\t\t\tfirstAStarted.resolve();",
				"\t\t\t\tawait releaseFirstA.promise;",
				'\t\t\t\tevents.push("a1-end");',
				"\t\t\t});",
				"\t\t\tawait firstAStarted.promise;",
				"\t\t\tconst secondA = withFileMutationQueue(pathA, async () => {",
				'\t\t\t\tevents.push("a2");',
				"\t\t\t});",
				"\t\t\tawait withFileMutationQueue(pathB, async () => {",
				'\t\t\t\tevents.push("b");',
				"\t\t\t});",
				'\t\t\tif (events.includes("a2")) throw new Error("same-path mutation overlapped");',
				"\t\t\treleaseFirstA.resolve();",
				"\t\t\tawait Promise.all([firstA, secondA]);",
				"\t\t\ttry {",
				"\t\t\t\tawait withFileMutationQueue(pathA, async () => {",
				'\t\t\t\t\tevents.push("throw");',
				'\t\t\t\t\tthrow new Error("expected queue rejection");',
				"\t\t\t\t});",
				'\t\t\t\tthrow new Error("queue callback did not reject");',
				"\t\t\t} catch (error) {",
				'\t\t\t\tif (!(error instanceof Error) || error.message !== "expected queue rejection") throw error;',
				"\t\t\t}",
				"\t\t\tawait withFileMutationQueue(pathA, async () => {",
				'\t\t\t\tevents.push("after-throw");',
				"\t\t\t});",
				'\t\t\treturn { content: [{ type: "text", text: events.join(",") }] };',
				"\t\t},",
				"\t});",
				"}",
			].join("\n"),
		);
	});

	afterEach(() => {
		projectDir.removeSync();
	});

	it("loads plugin extensions that still import legacy @mariozechner Pi packages", async () => {
		const result = await loadExtensions([extensionPath], projectDir.path());
		const extension = result.extensions.find(ext => ext.path === extensionPath);

		expect(result.errors).toEqual([]);
		expect(extension).toBeDefined();
		expect(extension?.commands.has("legacy-pi-ext")).toBe(true);
	});

	it("loads pi-codex-image-gen imports and preserves per-path queue behavior", async () => {
		const result = await loadExtensions([imageExtensionPath], projectDir.path());
		const extension = result.extensions.find(ext => ext.path === imageExtensionPath);
		const tool = extension?.tools.get("codex_generate_image");

		expect(result.errors).toEqual([]);
		expect(extension).toBeDefined();
		expect(tool).toBeDefined();
		if (!tool) throw new Error("codex_generate_image fixture tool was not registered");

		const toolResult = await tool.definition.execute(
			"fixture-call",
			{ prompt: "draw a deterministic probe", mode: "generate" },
			undefined,
			undefined,
			undefined as never,
		);

		expect(toolResult.content).toEqual([{ type: "text", text: "a1-start,b,a1-end,a2,throw,after-throw" }]);
	});
});
