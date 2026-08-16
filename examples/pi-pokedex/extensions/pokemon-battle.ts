/**
 * Pokemon Battle — a pi extension that teaches BOTH things extensions do:
 *
 *   1. ADD A VERB   → registerTool("type_matchup"): a typed, deterministic tool
 *                     the model can call directly. No network, no LLM guessing —
 *                     your TypeScript is the source of truth.
 *   2. GUARD A VERB → on("tool_call"): the "Team Rocket" guard blocks any bash
 *                     command that tries to wipe waku's runtime data (.waku, the
 *                     Pokemon Center). 10 lines = a working permission system.
 *
 * Load it for a quick test:   pi -e .pi/extensions/pokemon-battle.ts
 * Or drop it here (.pi/extensions/) and pi auto-discovers it after the project
 * is trusted.
 */

import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

// The real Gen-6+ offensive chart: attacking type -> types it hits for 2x.
// StringEnum (not Type.Union) keeps the parameter Google/Gemini-compatible.
const TYPES = [
	"normal", "fire", "water", "electric", "grass", "ice", "fighting", "poison",
	"ground", "flying", "psychic", "bug", "rock", "ghost", "dragon", "dark",
	"steel", "fairy",
] as const;

const SUPER_EFFECTIVE: Record<string, string[]> = {
	normal: [],
	fire: ["grass", "ice", "bug", "steel"],
	water: ["fire", "ground", "rock"],
	electric: ["water", "flying"],
	grass: ["water", "ground", "rock"],
	ice: ["grass", "ground", "flying", "dragon"],
	fighting: ["normal", "ice", "rock", "dark", "steel"],
	poison: ["grass", "fairy"],
	ground: ["fire", "electric", "poison", "rock", "steel"],
	flying: ["grass", "fighting", "bug"],
	psychic: ["fighting", "poison"],
	bug: ["grass", "psychic", "dark"],
	rock: ["fire", "ice", "flying", "bug"],
	ghost: ["psychic", "ghost"],
	dragon: ["dragon"],
	dark: ["psychic", "ghost"],
	steel: ["ice", "rock", "fairy"],
	fairy: ["fighting", "dragon", "dark"],
};

const typeMatchup = defineTool({
	name: "type_matchup",
	label: "Type matchup",
	description:
		"Given an attacking Pokemon type, return the types it is super-effective " +
		"against (deals 2x damage to). Use for battle strategy questions.",
	parameters: Type.Object({
		attacker: StringEnum(TYPES, { description: "The attacking move's type" }),
	}),
	async execute(_toolCallId, params) {
		const strong = SUPER_EFFECTIVE[params.attacker] ?? [];
		const text = strong.length
			? `${params.attacker} is super-effective (2x) against: ${strong.join(", ")}.`
			: `${params.attacker} has no super-effective matchups.`;
		return { content: [{ type: "text", text }], details: { attacker: params.attacker, strong } };
	},
});

// Team Rocket guard: hands off the Pokemon Center (.waku runtime data).
// Blocks the exact mistake waku's own rules forbid — wiping .waku or `rm -rf /`.
const ROCKET = /\brm\b[^\n]*\s(-[rRfF]+\s+)?(\.waku(\/|\b)|\/\s|~\/?\s*$)/;

export default function pokemonBattle(pi: ExtensionAPI) {
	pi.registerTool(typeMatchup);

	pi.on("tool_call", async (event) => {
		if (event.toolName !== "bash") return;
		const command = (event.input?.command as string) ?? "";
		if (ROCKET.test(command)) {
			return {
				block: true,
				reason:
					"Team Rocket guard: that command targets the Pokemon Center (.waku " +
					"runtime data / root). Blocked. Back up first or narrow the path.",
			};
		}
	});
}
