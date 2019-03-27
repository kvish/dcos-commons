FROM centos:7
MAINTAINER Mesosphere <help@mesosphere.io>

ARG HDFS_USER=hdfs
ARG HDFS_GROUP=hdfs

RUN groupadd -g 65534 nogroup \
    && usermod -u 99 -g 99 -c "Nobody user for RHEL based system" nobody \
    && echo "nobody:x:65534:65534:Nobody user for Debian based system:/nonexistent:/sbin/nologin" >> /etc/passwd \
    && usermod -a -G users nobody

RUN yum update -y -q && yum install -y -q yum-utils \
    ca-certificates wget curl

RUN wget http://dl.fedoraproject.org/pub/epel/epel-release-latest-7.noarch.rpm \
    && rpm -Uh epel-release-latest-7.noarch.rpm

RUN yum-config-manager --add-repo https://archive.cloudera.com/cdh6/6.1.1/redhat7/yum/ \
    && curl -O https://archive.cloudera.com/cdh6/6.1.1/redhat7/yum/RPM-GPG-KEY-cloudera \
    && mv RPM-GPG-KEY-cloudera /etc/pki/rpm-gpg/ \
    && rpm --import /etc/pki/rpm-gpg/RPM-GPG-KEY-cloudera

RUN yum update -y -q \
    && yum install -y -q hadoop

CMD [""]    
