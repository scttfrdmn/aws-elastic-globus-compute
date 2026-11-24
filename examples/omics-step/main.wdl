version 1.0

# A small HealthOmics private workflow for the aegc recipe: count the reads of one sample in a
# region, from the public 1000 Genomes CRAM, with the GRCh38 reference.
workflow region_reads {
  input {
    String region
    File cram = "s3://1000genomes/1000G_2504_high_coverage/data/ERR3239276/NA06985.final.cram"
    File crai = "s3://1000genomes/1000G_2504_high_coverage/data/ERR3239276/NA06985.final.cram.crai"
    File ref = "s3://1000genomes/technical/reference/GRCh38_reference_genome/GRCh38_full_analysis_set_plus_decoy_hla.fa"
    File ref_fai = "s3://1000genomes/technical/reference/GRCh38_reference_genome/GRCh38_full_analysis_set_plus_decoy_hla.fa.fai"
    String docker = "ECR_IMAGE_URI"
  }
  call count { input: region = region, cram = cram, crai = crai, ref = ref, ref_fai = ref_fai, docker = docker }
  output { File counts = count.counts }
}

task count {
  input {
    String region
    File cram
    File crai
    File ref
    File ref_fai
    String docker
  }
  command <<<
    samtools view -c -T ~{ref} -X ~{cram} ~{crai} ~{region} > counts.txt
  >>>
  runtime { docker: docker, cpu: 2, memory: "4 GiB" }
  output { File counts = "counts.txt" }
}
