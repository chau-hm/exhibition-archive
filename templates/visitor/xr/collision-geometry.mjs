const identityMatrix = () => [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];

function multiplyMatrix4(a, b) {
  const out = new Array(16).fill(0);
  for (let column = 0; column < 4; column++) {
    for (let row = 0; row < 4; row++) {
      for (let k = 0; k < 4; k++) {
        out[column * 4 + row] += a[k * 4 + row] * b[column * 4 + k];
      }
    }
  }
  return out;
}

function nodeMatrix(node) {
  if (Array.isArray(node.matrix) && node.matrix.length === 16) return node.matrix;
  const [x, y, z, w] = node.rotation ?? [0, 0, 0, 1];
  const [sx, sy, sz] = node.scale ?? [1, 1, 1];
  const [tx, ty, tz] = node.translation ?? [0, 0, 0];
  const x2 = x + x;
  const y2 = y + y;
  const z2 = z + z;
  const xx = x * x2;
  const xy = x * y2;
  const xz = x * z2;
  const yy = y * y2;
  const yz = y * z2;
  const zz = z * z2;
  const wx = w * x2;
  const wy = w * y2;
  const wz = w * z2;
  return [
    (1 - (yy + zz)) * sx, (xy + wz) * sx, (xz - wy) * sx, 0,
    (xy - wz) * sy, (1 - (xx + zz)) * sy, (yz + wx) * sy, 0,
    (xz + wy) * sz, (yz - wx) * sz, (1 - (xx + yy)) * sz, 0,
    tx, ty, tz, 1
  ];
}

function transformPoint(matrix, point) {
  const [x, y, z] = point;
  const w = matrix[3] * x + matrix[7] * y + matrix[11] * z + matrix[15];
  const inverseW = w && w !== 1 ? 1 / w : 1;
  return [
    (matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12]) * inverseW,
    (matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13]) * inverseW,
    (matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14]) * inverseW
  ];
}

function readAccessor(gltf, binary, index) {
  const accessor = gltf.accessors?.[index];
  const bufferView = gltf.bufferViews?.[accessor?.bufferView];
  if (!accessor || !bufferView || bufferView.buffer !== 0 || accessor.sparse) {
    throw new Error('The enclosure GLB has an unsupported accessor layout.');
  }
  const view = new DataView(binary);
  const base = (bufferView.byteOffset ?? 0) + (accessor.byteOffset ?? 0);
  const componentSizes = { 5121: 1, 5123: 2, 5125: 4, 5126: 4 };
  const componentSize = componentSizes[accessor.componentType];
  if (!componentSize) throw new Error('The enclosure GLB uses an unsupported component type.');
  const components = accessor.type === 'VEC3' ? 3 : accessor.type === 'SCALAR' ? 1 : 0;
  if (!components) throw new Error('The enclosure GLB uses an unsupported accessor type.');
  const stride = bufferView.byteStride ?? componentSize * components;
  const result = [];
  for (let i = 0; i < accessor.count; i++) {
    const values = [];
    for (let c = 0; c < components; c++) {
      const offset = base + i * stride + c * componentSize;
      let value;
      if (accessor.componentType === 5126) value = view.getFloat32(offset, true);
      else if (accessor.componentType === 5125) value = view.getUint32(offset, true);
      else if (accessor.componentType === 5123) value = view.getUint16(offset, true);
      else value = view.getUint8(offset);
      values.push(value);
    }
    result.push(components === 1 ? values[0] : values);
  }
  return result;
}

function edgeKey(a, b) {
  const key = point => point.map(value => Math.round(value * 100000)).join(',');
  const ka = key(a);
  const kb = key(b);
  return ka < kb ? ka + '|' + kb : kb + '|' + ka;
}

export function parseCollisionGlb(arrayBuffer) {
  const view = new DataView(arrayBuffer);
  if (view.byteLength < 20 || view.getUint32(0, true) !== 0x46546c67 || view.getUint32(4, true) !== 2) {
    throw new Error('The enclosure collision asset is not a glTF 2.0 binary file.');
  }
  let offset = 12;
  let gltf = null;
  let binary = null;
  while (offset + 8 <= view.byteLength) {
    const length = view.getUint32(offset, true);
    const type = view.getUint32(offset + 4, true);
    const start = offset + 8;
    if (start + length > view.byteLength) throw new Error('The enclosure GLB contains a truncated chunk.');
    if (type === 0x4e4f534a) {
      gltf = JSON.parse(new TextDecoder().decode(new Uint8Array(arrayBuffer, start, length)).trim());
    } else if (type === 0x004e4942) {
      binary = arrayBuffer.slice(start, start + length);
    }
    offset = start + length;
  }
  if (!gltf || !binary || gltf.buffers?.some(buffer => buffer.uri)) {
    throw new Error('The enclosure GLB is missing its embedded geometry.');
  }

  const triangles = [];
  const edgeCounts = new Map();
  const addMesh = (nodeIndex, parentMatrix) => {
    const node = gltf.nodes?.[nodeIndex];
    if (!node) throw new Error('The enclosure GLB has an invalid node reference.');
    const worldMatrix = multiplyMatrix4(parentMatrix, nodeMatrix(node));
    if (Number.isInteger(node.mesh)) {
      const mesh = gltf.meshes?.[node.mesh];
      for (const primitive of mesh?.primitives ?? []) {
        if ((primitive.mode ?? 4) !== 4 || !Number.isInteger(primitive.attributes?.POSITION)) {
          throw new Error('The enclosure GLB contains unsupported collision geometry.');
        }
        const positions = readAccessor(gltf, binary, primitive.attributes.POSITION)
          .map(point => transformPoint(worldMatrix, point));
        const indices = Number.isInteger(primitive.indices)
          ? readAccessor(gltf, binary, primitive.indices)
          : positions.map((_, i) => i);
        if (indices.length % 3 !== 0) throw new Error('The enclosure GLB triangle index list is incomplete.');
        for (let i = 0; i < indices.length; i += 3) {
          const a = positions[indices[i]];
          const b = positions[indices[i + 1]];
          const c = positions[indices[i + 2]];
          if (!a || !b || !c) throw new Error('The enclosure GLB references a missing vertex.');
          triangles.push([a, b, c]);
          for (const [p, q] of [[a, b], [b, c], [c, a]]) {
            const key = edgeKey(p, q);
            edgeCounts.set(key, (edgeCounts.get(key) ?? 0) + 1);
          }
        }
      }
    }
    for (const child of node.children ?? []) addMesh(child, worldMatrix);
  };

  const rootNodes = gltf.scenes?.[gltf.scene ?? 0]?.nodes
    ?? gltf.nodes?.map((node, index) => Number.isInteger(node.mesh) ? index : -1).filter(index => index >= 0)
    ?? [];
  for (const nodeIndex of rootNodes) addMesh(nodeIndex, identityMatrix());
  if (!triangles.length) throw new Error('The enclosure GLB contains no triangle mesh.');
  const openEdges = [...edgeCounts.values()].filter(count => count !== 2).length;
  if (openEdges) {
    throw new Error('The enclosure GLB is not a closed shell (' + openEdges + ' unmatched edges).');
  }
  const bounds = [[Infinity, -Infinity], [Infinity, -Infinity], [Infinity, -Infinity]];
  for (const triangle of triangles) {
    for (const point of triangle) {
      for (let axis = 0; axis < 3; axis++) {
        bounds[axis][0] = Math.min(bounds[axis][0], point[axis]);
        bounds[axis][1] = Math.max(bounds[axis][1], point[axis]);
      }
    }
  }
  return { triangles, bounds, watertight: true };
}

function dot3(a, b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

function subtract3(a, b) {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}

function cross3(a, b) {
  return [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0]
  ];
}

function pointSegmentDistanceSquared(point, a, b) {
  const ab = subtract3(b, a);
  const lengthSquared = dot3(ab, ab);
  const t = lengthSquared > 0
    ? Math.max(0, Math.min(1, dot3(subtract3(point, a), ab) / lengthSquared))
    : 0;
  const closest = [a[0] + ab[0] * t, a[1] + ab[1] * t, a[2] + ab[2] * t];
  const delta = subtract3(point, closest);
  return dot3(delta, delta);
}

function pointTriangleDistanceSquared(point, triangle) {
  const [a, b, c] = triangle;
  const ab = subtract3(b, a);
  const ac = subtract3(c, a);
  const normal = cross3(ab, ac);
  const normalSquared = dot3(normal, normal);
  if (normalSquared < 1e-16) {
    return Math.min(
      pointSegmentDistanceSquared(point, a, b),
      pointSegmentDistanceSquared(point, b, c),
      pointSegmentDistanceSquared(point, c, a)
    );
  }
  const signed = dot3(subtract3(point, a), normal);
  const projected = [
    point[0] - normal[0] * signed / normalSquared,
    point[1] - normal[1] * signed / normalSquared,
    point[2] - normal[2] * signed / normalSquared
  ];
  const v0 = ab;
  const v1 = ac;
  const v2 = subtract3(projected, a);
  const d00 = dot3(v0, v0);
  const d01 = dot3(v0, v1);
  const d11 = dot3(v1, v1);
  const d20 = dot3(v2, v0);
  const d21 = dot3(v2, v1);
  const denominator = d00 * d11 - d01 * d01;
  const v = (d11 * d20 - d01 * d21) / denominator;
  const w = (d00 * d21 - d01 * d20) / denominator;
  const u = 1 - v - w;
  if (u >= 0 && v >= 0 && w >= 0) return signed * signed / normalSquared;
  return Math.min(
    pointSegmentDistanceSquared(point, a, b),
    pointSegmentDistanceSquared(point, b, c),
    pointSegmentDistanceSquared(point, c, a)
  );
}

function rayIntersectionDistance(point, direction, triangle) {
  const [a, b, c] = triangle;
  const edge1 = subtract3(b, a);
  const edge2 = subtract3(c, a);
  const h = cross3(direction, edge2);
  const determinant = dot3(edge1, h);
  if (Math.abs(determinant) < 1e-10) return null;
  const inverse = 1 / determinant;
  const s = subtract3(point, a);
  const u = inverse * dot3(s, h);
  if (u < -1e-8 || u > 1 + 1e-8) return null;
  const q = cross3(s, edge1);
  const v = inverse * dot3(direction, q);
  if (v < -1e-8 || u + v > 1 + 1e-8) return null;
  const distance = inverse * dot3(edge2, q);
  return distance > 1e-7 ? distance : null;
}

export function createCollisionGuard(mesh, radius = 0.2) {
  if (!mesh?.watertight || !mesh.triangles?.length || !Number.isFinite(radius) || radius <= 0) {
    throw new Error('A closed collision mesh and positive camera radius are required.');
  }
  const directions = [
    [1, 0.3713906763541037, 0.694202323231],
    [0.217348, 1, 0.493127],
    [0.463117, 0.283719, 1]
  ].map(direction => {
    const length = Math.hypot(...direction);
    return direction.map(value => value / length);
  });

  const isInside = point => {
    let insideVotes = 0;
    for (const direction of directions) {
      const hits = [];
      for (const triangle of mesh.triangles) {
        const distance = rayIntersectionDistance(point, direction, triangle);
        if (distance !== null) hits.push(distance);
      }
      hits.sort((a, b) => a - b);
      let uniqueHits = 0;
      let previous = -Infinity;
      for (const hit of hits) {
        if (hit - previous > 1e-5) {
          uniqueHits++;
          previous = hit;
        }
      }
      if (uniqueHits % 2 === 1) insideVotes++;
    }
    return insideVotes >= 2;
  };

  const minimumDistanceSquared = point => {
    let result = Infinity;
    for (const triangle of mesh.triangles) {
      result = Math.min(result, pointTriangleDistanceSquared(point, triangle));
    }
    return result;
  };

  const inspect = point => {
    if (!Array.isArray(point) || point.length !== 3 || !point.every(Number.isFinite)) {
      return {
        valid: false,
        inside: false,
        boundaryDistance: null,
        clearance: null,
        requiredClearance: radius,
        reason: 'invalid-position'
      };
    }
    const inside = isInside(point);
    const boundaryDistance = Math.sqrt(minimumDistanceSquared(point));
    const clearance = inside ? boundaryDistance - radius : -boundaryDistance;
    const valid = inside && clearance >= -1e-5;
    return {
      valid,
      inside,
      boundaryDistance,
      clearance,
      requiredClearance: radius,
      reason: valid ? null : inside ? 'within collision radius' : 'outside enclosure'
    };
  };

  const isValid = point => inspect(point).valid;

  const segmentAllowed = (from, to) => {
    if (!isValid(from) || !isValid(to)) {
      return { allowed: false, reason: 'outside enclosure or within collision radius' };
    }
    const distance = Math.hypot(to[0] - from[0], to[1] - from[1], to[2] - from[2]);
    const step = Math.max(0.02, Math.min(0.05, radius * 0.25));
    const count = Math.max(1, Math.ceil(distance / step));
    for (let i = 1; i < count; i++) {
      const t = i / count;
      const point = [
        from[0] + (to[0] - from[0]) * t,
        from[1] + (to[1] - from[1]) * t,
        from[2] + (to[2] - from[2]) * t
      ];
      if (!isValid(point)) {
        return { allowed: false, reason: 'swept path crosses the enclosure or its clearance margin' };
      }
    }
    return { allowed: true, reason: null };
  };

  return {
    radius,
    triangleCount: mesh.triangles.length,
    bounds: mesh.bounds,
    isInside,
    inspect,
    isValid,
    segmentAllowed
  };
}
