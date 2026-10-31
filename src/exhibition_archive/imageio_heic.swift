import Foundation
import ImageIO
import UniformTypeIdentifiers

struct ConversionRequest: Decodable {
    let source: String
    let output: String
}

func nested(_ properties: [CFString: Any], _ dictionary: CFString, _ key: CFString) -> Any? {
    (properties[dictionary] as? [CFString: Any])?[key]
}

func canonical(_ value: Any) -> String {
    if let value = value as? String { return "s:" + value }
    if let value = value as? NSNumber { return "n:" + value.stringValue }
    if let value = value as? Data { return "d:" + value.base64EncodedString() }
    if let value = value as? [Any] { return "a:[" + value.map(canonical).joined(separator: ",") + "]" }
    if let value = value as? [String: Any] {
        return "o:{" + value.keys.sorted().map { "\($0)=\(canonical(value[$0] ?? NSNull()))" }.joined(separator: ",") + "}"
    }
    return "v:" + String(describing: value)
}

struct MetadataField {
    let name: String
    let dictionary: CFString
    let key: CFString
}

let metadataFields: [MetadataField] = [
    MetadataField(name: "camera_make", dictionary: kCGImagePropertyTIFFDictionary, key: kCGImagePropertyTIFFMake),
    MetadataField(name: "camera_model", dictionary: kCGImagePropertyTIFFDictionary, key: kCGImagePropertyTIFFModel),
    MetadataField(name: "lens_make", dictionary: kCGImagePropertyExifDictionary, key: kCGImagePropertyExifLensMake),
    MetadataField(name: "lens_model", dictionary: kCGImagePropertyExifDictionary, key: kCGImagePropertyExifLensModel),
    MetadataField(name: "focal_length", dictionary: kCGImagePropertyExifDictionary, key: kCGImagePropertyExifFocalLength),
    MetadataField(name: "focal_length_35mm", dictionary: kCGImagePropertyExifDictionary, key: kCGImagePropertyExifFocalLenIn35mmFilm),
    MetadataField(name: "f_number", dictionary: kCGImagePropertyExifDictionary, key: kCGImagePropertyExifFNumber),
    MetadataField(name: "exposure_time", dictionary: kCGImagePropertyExifDictionary, key: kCGImagePropertyExifExposureTime),
    MetadataField(name: "iso_speed_ratings", dictionary: kCGImagePropertyExifDictionary, key: kCGImagePropertyExifISOSpeedRatings),
    MetadataField(name: "focal_plane_x_resolution", dictionary: kCGImagePropertyExifDictionary, key: kCGImagePropertyExifFocalPlaneXResolution),
    MetadataField(name: "focal_plane_y_resolution", dictionary: kCGImagePropertyExifDictionary, key: kCGImagePropertyExifFocalPlaneYResolution),
    MetadataField(name: "focal_plane_resolution_unit", dictionary: kCGImagePropertyExifDictionary, key: kCGImagePropertyExifFocalPlaneResolutionUnit),
    MetadataField(name: "date_time_original", dictionary: kCGImagePropertyExifDictionary, key: kCGImagePropertyExifDateTimeOriginal),
    MetadataField(name: "date_time_digitized", dictionary: kCGImagePropertyExifDictionary, key: kCGImagePropertyExifDateTimeDigitized),
    MetadataField(name: "gps_latitude", dictionary: kCGImagePropertyGPSDictionary, key: kCGImagePropertyGPSLatitude),
    MetadataField(name: "gps_latitude_ref", dictionary: kCGImagePropertyGPSDictionary, key: kCGImagePropertyGPSLatitudeRef),
    MetadataField(name: "gps_longitude", dictionary: kCGImagePropertyGPSDictionary, key: kCGImagePropertyGPSLongitude),
    MetadataField(name: "gps_longitude_ref", dictionary: kCGImagePropertyGPSDictionary, key: kCGImagePropertyGPSLongitudeRef),
    MetadataField(name: "gps_altitude", dictionary: kCGImagePropertyGPSDictionary, key: kCGImagePropertyGPSAltitude),
    MetadataField(name: "gps_altitude_ref", dictionary: kCGImagePropertyGPSDictionary, key: kCGImagePropertyGPSAltitudeRef),
    MetadataField(name: "gps_date_stamp", dictionary: kCGImagePropertyGPSDictionary, key: kCGImagePropertyGPSDateStamp),
    MetadataField(name: "gps_time_stamp", dictionary: kCGImagePropertyGPSDictionary, key: kCGImagePropertyGPSTimeStamp),
]

func converted(_ request: ConversionRequest) throws -> [String: Any] {
    let sourceURL = URL(fileURLWithPath: request.source)
    let outputURL = URL(fileURLWithPath: request.output)
    guard let source = CGImageSourceCreateWithURL(sourceURL as CFURL, nil),
          let sourceImage = CGImageSourceCreateImageAtIndex(source, 0, nil),
          let sourceProperties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any] else {
        throw NSError(domain: "ExhibitionArchive.ImageIO", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "ImageIO could not decode the HEIF source or read its metadata"])
    }

    let orientation = (sourceProperties[kCGImagePropertyOrientation] as? NSNumber)?.intValue ?? 1
    guard (1...8).contains(orientation) else {
        throw NSError(domain: "ExhibitionArchive.ImageIO", code: 2,
                      userInfo: [NSLocalizedDescriptionKey: "Source EXIF orientation is outside the supported range 1–8"])
    }

    // ImageIO applies EXIF orientation to the decoded pixels. The maximum side
    // equals the stored source side, so this transformation does not resize.
    let maxSide = max(sourceImage.width, sourceImage.height)
    let thumbnailOptions = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: maxSide,
        kCGImageSourceShouldCacheImmediately: true,
    ] as CFDictionary
    guard let uprightImage = CGImageSourceCreateThumbnailAtIndex(source, 0, thumbnailOptions) else {
        throw NSError(domain: "ExhibitionArchive.ImageIO", code: 3,
                      userInfo: [NSLocalizedDescriptionKey: "ImageIO could not decode and normalize source orientation"])
    }

    var outputProperties = sourceProperties
    outputProperties[kCGImagePropertyOrientation] = NSNumber(value: 1)
    if var tiff = outputProperties[kCGImagePropertyTIFFDictionary] as? [CFString: Any] {
        tiff[kCGImagePropertyTIFFOrientation] = NSNumber(value: 1)
        outputProperties[kCGImagePropertyTIFFDictionary] = tiff
    }
    if var exif = outputProperties[kCGImagePropertyExifDictionary] as? [CFString: Any] {
        exif[kCGImagePropertyExifPixelXDimension] = NSNumber(value: uprightImage.width)
        exif[kCGImagePropertyExifPixelYDimension] = NSNumber(value: uprightImage.height)
        outputProperties[kCGImagePropertyExifDictionary] = exif
    }
    outputProperties[kCGImageDestinationLossyCompressionQuality] = NSNumber(value: 0.95)

    guard let destination = CGImageDestinationCreateWithURL(outputURL as CFURL,
                                                              UTType.jpeg.identifier as CFString,
                                                              1, nil) else {
        throw NSError(domain: "ExhibitionArchive.ImageIO", code: 4,
                      userInfo: [NSLocalizedDescriptionKey: "ImageIO could not create a JPEG destination"])
    }
    CGImageDestinationAddImage(destination, uprightImage, outputProperties as CFDictionary)
    guard CGImageDestinationFinalize(destination) else {
        throw NSError(domain: "ExhibitionArchive.ImageIO", code: 5,
                      userInfo: [NSLocalizedDescriptionKey: "ImageIO could not finalize the JPEG output"])
    }

    guard let outputSource = CGImageSourceCreateWithURL(outputURL as CFURL, nil),
          let outputImage = CGImageSourceCreateImageAtIndex(outputSource, 0, nil),
          let verifiedProperties = CGImageSourceCopyPropertiesAtIndex(outputSource, 0, nil) as? [CFString: Any] else {
        throw NSError(domain: "ExhibitionArchive.ImageIO", code: 6,
                      userInfo: [NSLocalizedDescriptionKey: "ImageIO could not reopen the written JPEG"])
    }

    let expectedWidth = (5...8).contains(orientation) ? sourceImage.height : sourceImage.width
    let expectedHeight = (5...8).contains(orientation) ? sourceImage.width : sourceImage.height
    guard outputImage.width == expectedWidth && outputImage.height == expectedHeight else {
        throw NSError(domain: "ExhibitionArchive.ImageIO", code: 7,
                      userInfo: [NSLocalizedDescriptionKey: "JPEG dimensions do not match the orientation-normalized source dimensions"])
    }
    let outputOrientation = (verifiedProperties[kCGImagePropertyOrientation] as? NSNumber)?.intValue ?? 1
    let outputTIFFOrientation = (nested(verifiedProperties, kCGImagePropertyTIFFDictionary,
                                        kCGImagePropertyTIFFOrientation) as? NSNumber)?.intValue ?? 1
    guard outputOrientation == 1 && outputTIFFOrientation == 1 else {
        throw NSError(domain: "ExhibitionArchive.ImageIO", code: 8,
                      userInfo: [NSLocalizedDescriptionKey: "JPEG pixels are upright but EXIF orientation was not flattened to 1"])
    }

    var sourceTags: [String] = []
    var retainedTags: [String] = []
    var missingTags: [String] = []
    for field in metadataFields {
        guard let expected = nested(sourceProperties, field.dictionary, field.key) else { continue }
        sourceTags.append(field.name)
        if let actual = nested(verifiedProperties, field.dictionary, field.key), canonical(actual) == canonical(expected) {
            retainedTags.append(field.name)
        } else {
            missingTags.append(field.name)
        }
    }
    guard missingTags.isEmpty else {
        throw NSError(domain: "ExhibitionArchive.ImageIO", code: 9,
                      userInfo: [NSLocalizedDescriptionKey: "JPEG did not retain source EXIF fields: \(missingTags.joined(separator: ", "))"])
    }

    let sourceProfile = (sourceProperties[kCGImagePropertyProfileName] as? String) ??
        (sourceImage.colorSpace?.name as String?)
    let outputProfile = (verifiedProperties[kCGImagePropertyProfileName] as? String) ??
        (outputImage.colorSpace?.name as String?)
    let sourceICC = sourceImage.colorSpace?.copyICCData() as Data?
    let outputICC = outputImage.colorSpace?.copyICCData() as Data?
    if let sourceICC {
        guard let outputICC, outputICC == sourceICC else {
            throw NSError(domain: "ExhibitionArchive.ImageIO", code: 12,
                          userInfo: [NSLocalizedDescriptionKey: "JPEG did not preserve the source ICC profile bytes"])
        }
    }
    if let sourceProfile {
        guard let outputProfile, sourceProfile == outputProfile else {
            throw NSError(domain: "ExhibitionArchive.ImageIO", code: 10,
                          userInfo: [NSLocalizedDescriptionKey: "JPEG did not retain the decoded source color profile name"])
        }
    }
    let colorStatus: String
    if sourceICC != nil {
        colorStatus = "ICC_PROFILE_BYTES_PRESERVED"
    } else if sourceProfile != nil {
        colorStatus = "PROFILE_NAME_MATCHED_NO_SOURCE_ICC_BYTES"
    } else {
        colorStatus = "SOURCE_COLOR_PROFILE_UNKNOWN"
    }
    let missingEssential: [String] = [
        (nested(sourceProperties, kCGImagePropertyTIFFDictionary, kCGImagePropertyTIFFMake) == nil) ? "camera_make" : nil,
        (nested(sourceProperties, kCGImagePropertyTIFFDictionary, kCGImagePropertyTIFFModel) == nil) ? "camera_model" : nil,
        (nested(sourceProperties, kCGImagePropertyExifDictionary, kCGImagePropertyExifFocalLength) == nil &&
         nested(sourceProperties, kCGImagePropertyExifDictionary, kCGImagePropertyExifFocalLenIn35mmFilm) == nil) ? "focal_length" : nil,
    ].compactMap { $0 }
    guard missingEssential.isEmpty else {
        throw NSError(domain: "ExhibitionArchive.ImageIO", code: 11,
                      userInfo: [NSLocalizedDescriptionKey: "HEIF source lacks essential camera metadata for SfM: \(missingEssential.joined(separator: ", "))"])
    }

    return [
        "source_width": sourceImage.width,
        "source_height": sourceImage.height,
        "source_orientation": orientation,
        "width": outputImage.width,
        "height": outputImage.height,
        "output_orientation": outputOrientation,
        "output_tiff_orientation": outputTIFFOrientation,
        "jpeg_quality": 95,
        "source_metadata_fields": sourceTags,
        "retained_metadata_fields": retainedTags,
        "metadata_validation_status": sourceTags.isEmpty ? "NO_RELEVANT_SOURCE_METADATA" : "PRESERVED_SOURCE_FIELDS",
        "missing_source_essential_fields": missingEssential,
        "source_color_profile": sourceProfile as Any? ?? NSNull(),
        "output_color_profile": outputProfile as Any? ?? NSNull(),
        "color_profile_validation_status": colorStatus,
        "source_icc_profile_byte_count": sourceICC?.count as Any? ?? NSNull(),
        "output_icc_profile_byte_count": outputICC?.count as Any? ?? NSNull(),
        "icc_profile_validation_status": sourceICC == nil ? "NO_SOURCE_ICC_PROFILE_BYTES" : "ICC_PROFILE_BYTES_PRESERVED",
        "color_handling": "ImageIO decode and orientation normalization; source properties copied to JPEG; ICC bytes checked when present, otherwise reported color-space names compared; no explicit gamut relabel or color conversion",
    ]
}

let requestURL = URL(fileURLWithPath: CommandLine.arguments[1])
let requestData = try Data(contentsOf: requestURL)
let requests = try JSONDecoder().decode([ConversionRequest].self, from: requestData)
var results: [[String: Any]] = []
var failed = false
for request in requests {
    do {
        results.append(["source": request.source, "output": request.output,
                        "status": "VERIFIED", "details": try converted(request)])
    } catch {
        failed = true
        results.append(["source": request.source, "output": request.output,
                        "status": "FAILED", "error": error.localizedDescription])
    }
}
let resultData = try JSONSerialization.data(withJSONObject: ["results": results], options: [.sortedKeys])
print(String(data: resultData, encoding: .utf8) ?? "{}")
if failed { exit(EXIT_FAILURE) }
