import * as fs from "node:fs/promises";
import * as path from "node:path";
import { isEnoent, isEnotdir } from "@oh-my-pi/pi-utils";

const mutationQueues = new Map<string, Promise<void>>();
let registrationQueue = Promise.resolve();

async function canonicalizeFilePath(filePath: string): Promise<string> {
	try {
		return await fs.realpath(filePath);
	} catch (error) {
		if (isEnoent(error) || isEnotdir(error)) return path.resolve(filePath);
		throw error;
	}
}

/** Serialize file mutations per canonical path while allowing unrelated paths to proceed independently. */
export async function withFileMutationQueue<T>(filePath: string, fn: () => Promise<T>): Promise<T> {
	const canonicalPath = await canonicalizeFilePath(filePath);
	const registration = Promise.withResolvers<void>();
	const previousRegistration = registrationQueue;
	registrationQueue = registration.promise;

	await previousRegistration;
	const previousMutation = mutationQueues.get(canonicalPath) ?? Promise.resolve();
	const currentMutation = Promise.withResolvers<void>();
	mutationQueues.set(canonicalPath, currentMutation.promise);
	registration.resolve();

	await previousMutation;
	try {
		return await fn();
	} finally {
		currentMutation.resolve();
		if (mutationQueues.get(canonicalPath) === currentMutation.promise) {
			mutationQueues.delete(canonicalPath);
		}
	}
}
